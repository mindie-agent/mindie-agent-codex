"""Static discovery plus host-identity-bound, bounded, one-shot runtime calls.

[Role]
Implements the JSON-RPC MCP server gate (`Gate`, `serve`) for both `knowledge` and `remote` surfaces, including native `mindie_interrupt` cancellation and `RemoteReceipts` replay protection.

[Invariants]
- Remote tools are available to any native Codex task without a knowledge lease, scoped by `REMOTE_DEV_SESSION_ID`.
- Knowledge mutation tools (`knowledge_feedback`) require an active task lease; read tools (`knowledge_query`, `knowledge_explain`) work without capture binding.
- When a remote operation has already succeeded (`outcome == "succeeded"`), a local receipt-accounting failure NEVER overwrites the business result with `isError=True` (`receipt_unconfirmed=True` is attached instead).

[Timeouts & Locks]
- Uses shared `update_lock` around runtime subprocess dispatch (`runtime_call.py`), SQLite WAL mode with `busy_timeout=5000` for `RemoteReceipts`, and per-request cancellation events triggered by `notifications/cancelled` or `mindie_interrupt`.

[Side-Effect Policy]
- `initialize` and `tools/list` are pure in-memory catalog reads (`mcp_catalog.json`) that never touch disk or start services.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import sqlite3
import time
import threading
import uuid
import weakref

from bounded_process import run, ProcessResult
from diagnostic_support import attach as attach_diagnostic
from diagnostic_support import failure as diagnostic_failure
from session_gate import (
    IDENTITY, Sessions, config_path,
    runtime_env, runtime_scripts,
)
from update_lock import update_lock, file_lock
from receipt_layout import prepare_layout
import receipt_layout

# Knowledge stdout only. A legal maximum page measured 817407 bytes.
KNOWLEDGE_MAX_OUTPUT = 1024 * 1024
CATALOG = Path(__file__).with_name("mcp_catalog.json")
INTERRUPT_TOOL = "mindie_interrupt"
RECEIPT_SCHEMA = {
    "authority": ("table", "authority", "CREATE TABLE authority(identity TEXT PRIMARY KEY, schema TEXT NOT NULL)"),
    "attempts": ("table", "attempts", "CREATE TABLE attempts (identity TEXT PRIMARY KEY, started REAL NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL)"),
    "attempts_running_owner": ("index", "attempts", "CREATE INDEX attempts_running_owner ON attempts(owner) WHERE status='running'"),
}


def failure(message):
    return dict(content=[dict(type="text", text=message)], isError=True)


class InvalidToolArguments(ValueError):
    """Rejected by the declared tool schema before any runtime dispatch."""


class SwitchingRuntime(RuntimeError):
    """The operation lock was unavailable before any request admission."""


def call_failure(exc):
    if isinstance(exc, SwitchingRuntime):
        return dict(failure("MindIE is switching its idle runtime; this call has not started."),
                    structuredContent=dict(code="temporary_unavailable",
                                           execution="not_started", automatic_retry=False))
    if isinstance(exc, InvalidToolArguments):
        message = (f"Request rejected before execution: {str(exc)[:240]}. "
                   "Correct the arguments once using the tool schema; "
                   "do not repeat unchanged arguments. No automatic retry.")
        return dict(failure(message), structuredContent=dict(
            code="invalid_arguments", execution="not_started",
            automatic_retry=False, message=message,
        ))
    result = failure(f"{type(exc).__name__}: {str(exc)[:240]}. No automatic retry.")
    process = getattr(exc, "process_result", None)
    if isinstance(process, ProcessResult):
        result["process"] = dict(execution=process.execution, returncode=process.returncode,
                                 cleanup=process.cleanup, automatic_retry=False)
    return result


def _reader_only_cleanup(issues):
    return bool(issues) and all(
        isinstance(item, dict) and (
            item.get("stage") in {"join_reader", "close_pipe", "reader_join_timed_out"}
            or item.get("error_type") == "ReaderStillRunning"
        )
        for item in issues
    )


def runtime_result(completed):
    """A helper's valid business envelope survives its separate cleanup fault."""
    if not isinstance(completed, ProcessResult) or completed.execution != "completed":
        raise ValueError("runtime helper has no completed process result")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        raise ValueError("Invalid MindIE runtime response")
    if completed.cleanup:
        if completed.returncode == 0 and _reader_only_cleanup(completed.cleanup):
            result = dict(
                result,
                operation_outcome="failed" if result.get("isError") is True else "succeeded",
                cleanup_warning=dict(
                    status="warning", issues=completed.cleanup,
                    operation_outcome="preserved", automatic_retry=False,
                ),
            )
            return result
        result = dict(result, operation_outcome="failed" if result.get("isError") is True else "succeeded", isError=True, cleanup=dict(
            status="failed", issues=completed.cleanup, operation_outcome="preserved",
            automatic_retry=False))
        result["content"].append(dict(type="text", text=
            "Runtime operation result preserved; local process cleanup failed. Do not repeat the operation."))
    return result


def helper_failure(result, diagnostic, stage, category):
    """Name the local failure boundary without claiming business execution state."""
    result = attach_diagnostic(result, diagnostic)
    result["structuredContent"] = dict(
        component="mindie-agent-codex", stage=stage, code=category,
        execution="outcome_unconfirmed", automatic_retry=False,
        diagnostic=result.get("diagnostic", {}),
    )
    result["content"].append(dict(
        type="text",
        text=f"The local MindIE runtime helper failed at {stage} ({category}). "
             "The business operation's outcome is unconfirmed; no automatic retry.",
    ))
    return result


def remote_state_dir():
    """Independent remote state root under the local user data root.

    Never the knowledge engine root. Static discovery does not create this;
    only an actual remote call may, and only for its own bounded receipt,
    and job-ownership bookkeeping.
    """
    override = os.environ.get("MINDIE_REMOTE_STATE_DIR")
    if override:
        return Path(override).expanduser().absolute()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base).expanduser().absolute() / "mindie-remote-dev"


def native_identity(request):
    """Bind one tools/call to the native thread, not the session tree.

    Nested thread_id, nested session_id, and top-level threadId are required
    valid identity strings. thread_id must equal threadId. session_id names
    the session tree and is not required to equal the thread. Top-level
    sessionId is optional; when the key is present, null included, it must
    be a valid id equal to nested session_id. The 0.153.4 root probe showed
    equal ids on that one root task. It does not establish child metadata.
    Tool arguments and a parent lease are never fallbacks.
    """
    params = request.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("arguments"), dict):
        raise ValueError("Invalid MindIE tool arguments")
    meta = params.get("_meta")
    turn = meta.get("x-codex-turn-metadata") if isinstance(meta, dict) else None
    thread = turn.get("thread_id") if isinstance(turn, dict) else None
    session = turn.get("session_id") if isinstance(turn, dict) else None
    plain = meta.get("threadId") if isinstance(meta, dict) else None
    if not all(isinstance(value, str) and IDENTITY.fullmatch(value)
               for value in (thread, session, plain) if value is not None):
        raise ValueError("Invalid native task identity metadata")
    if thread is None or session is None or plain is None:
        raise ValueError(
            "This host does not deliver native task identity metadata; MindIE "
            "requires a Codex version with turn metadata on tools/call and "
            "fails closed here — do not retry or supply an identity by hand"
        )
    if thread != plain:
        raise ValueError(
            "Contradictory native task identity metadata; call rejected"
        )
    if isinstance(meta, dict) and "sessionId" in meta:
        top_session = meta.get("sessionId")
        if not isinstance(top_session, str) or not IDENTITY.fullmatch(top_session):
            raise ValueError("Invalid native task identity metadata")
        if top_session != session:
            raise ValueError(
                "Contradictory native task identity metadata; call rejected"
            )
    return thread


def native_turn_identity(request):
    thread = native_identity(request)
    turn = request["params"]["_meta"]["x-codex-turn-metadata"].get("turn_id")
    if not isinstance(turn, str) or not IDENTITY.fullmatch(turn):
        raise ValueError("Invalid native turn identity metadata")
    return thread, turn


def interrupt_turn(request, pending):
    """Signal existing calls only; the native Hook does not assert completion."""
    params = request.get("params")
    arguments = params.get("arguments") if isinstance(params, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(arguments, dict) or set(arguments) != {"thread_id", "turn_id"}:
        raise ValueError("Invalid native interrupt identity")
    scope = arguments["thread_id"], arguments["turn_id"]
    if not all(isinstance(value, str) and IDENTITY.fullmatch(value) for value in scope):
        raise ValueError("Invalid native interrupt identity")
    if not isinstance(meta, dict) or meta.get("threadId") != scope[0]:
        raise ValueError("Contradictory native interrupt identity")
    if "x-codex-turn-metadata" in meta and native_turn_identity(request) != scope:
        raise ValueError("Contradictory native interrupt identity")
    for event, identity in pending.values():
        if identity == scope:
            event.set()
    return dict(content=[dict(type="text", text="{}")], isError=False)


class RemoteReceipts:
    """Durable task-local admission, without a knowledge lease or service.

    SQLite rejects corrupt state instead of reopening an empty replay ledger.
    Request keys remain on disk, never in an unbounded in-memory collection;
    there is no lifetime call ceiling or eviction that makes old keys reusable.
    A failed operation never prevents a later independent diagnostic call.
    Uncertain effects remain consumed receipts and cannot be replayed.
    """

    def __init__(self, session):
        # Selection and compatibility are checked under the initialization
        # lock in _db; another process may be publishing the first layout.
        self.path = remote_state_dir() / ('gate-v' + str(receipt_layout.FORMAT)) / (session + ".sqlite3")
        self.owner = uuid.uuid4().hex
        self._ownership = ExitStack()
        self._owner_ready = False
        weakref.finalize(self, self._ownership.close)

    def _ensure_owner(self):
        if not self._owner_ready:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._ownership.enter_context(file_lock(self.path.parent / (self.owner + ".owner.lock")))
            self._owner_ready = True

    def _owner_alive(self, owner):
        if not isinstance(owner, str) or len(owner) != 32 or any(c not in "0123456789abcdef" for c in owner):
            raise ValueError("remote receipt owner identity is invalid")
        if owner == self.owner:
            return self._owner_ready
        try:
            with file_lock(self.path.parent / (owner + ".owner.lock"), exclusive=True):
                return False
        except BlockingIOError:
            return True

    def _db(self):
        db = None
        try:
            with prepare_layout(remote_state_dir()) as directory:
                if self.path.parent != directory:
                    raise RuntimeError("remote receipt layout changed during admission")
                db = self._open_db()
            return db
        except BaseException as error:
            if db is not None:
                try:
                    db.close()
                except Exception as cleanup:
                    error.add_note('Receipt initialization cleanup also failed: ' + type(cleanup).__name__)
            raise

    def _open_db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(self.path.with_suffix(".initialize.lock"), exclusive=True, blocking=True):
            existed = self.path.exists()
            db = sqlite3.connect(self.path, timeout=5.0)
            try:
                os.chmod(self.path, 0o600)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=5000")
                if not existed:
                    with db:
                        for _, _, definition in RECEIPT_SCHEMA.values():
                            db.execute(definition)
                        db.execute("INSERT INTO authority VALUES(?,?)", (uuid.uuid4().hex, "mindie-remote-receipts/1"))
                else:
                    row = db.execute("SELECT identity,schema FROM authority").fetchone()
                    if not row or len(row[0]) != 32 or row[1] != "mindie-remote-receipts/1":
                        raise ValueError("remote receipt authority identity differs")
                db.execute("PRAGMA cache_size=-2048")
                return db
            except BaseException:
                db.close()
                raise

    def claim(self, identity):
        db = self._db()
        try:
            self._ensure_owner()
            with db:
                db.execute("BEGIN IMMEDIATE")
                # Actual owner death leaves an uncertain consumed receipt.
                # Elapsed time and earlier failures never reject new work.
                for owner, in db.execute("SELECT DISTINCT owner FROM attempts WHERE status='running'").fetchall():
                    if not self._owner_alive(owner):
                        db.execute("UPDATE attempts SET status='unknown' WHERE status='running' AND owner=?", (owner,))
                if db.execute("SELECT 1 FROM attempts WHERE identity=?", (identity,)).fetchone():
                    return False
                if db.execute("SELECT count(*) FROM attempts WHERE status='running'").fetchone()[0] >= 4:
                    raise ValueError("Remote task concurrency limit reached; no automatic retry")
                db.execute("INSERT INTO attempts(identity,started,status,owner) VALUES(?, ?, 'running', ?)", (identity, time.time(), self.owner))
                return True
        finally:
            db.close()

    def finish(self, identity, outcome):
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid remote operation outcome")
        db = self._db()
        try:
            with db:
                changed = db.execute("UPDATE attempts SET status=? WHERE identity=? AND status='running' AND owner=?",
                                     (outcome, identity, self.owner))
                if changed.rowcount != 1:
                    raise ValueError("remote outcome receipt no longer belongs to this owner")
        finally:
            db.close()


def _adapter_config():
    path = config_path()
    from product_contract import read_json
    return read_json(path)[0]


class Gate:
    def __init__(self, surface):
        self.surface = surface
        self.sessions = Sessions() if surface == "knowledge" else None
        self.tools = json.loads(CATALOG.read_text(encoding='utf-8'))[surface]
        self.connection_id = uuid.uuid4().hex
        self._remote_receipts = {}
        self.config = config_path()

    def call(self, request, cancel=None, *, timeout=None, cli_identity=None):
        try:
            with ExitStack() as lock:
                try:
                    lock.enter_context(update_lock(self.config))
                except BlockingIOError:
                    raise SwitchingRuntime() from None
                if self.config != config_path():
                    raise ValueError("this MCP process is bound to another installation profile")
                if self.surface == "remote":
                    return self._remote_call(request, cancel, timeout, cli_identity)
                # Discovery creates no capture state. Task identity comes from
                # the native metadata or the trusted CLI, never tool arguments.
                session = native_identity(request) if cli_identity is None else cli_identity[0]
                return self._call(request, session, cancel, timeout=timeout)
        except Exception as exc:
            return call_failure(exc)

    def _tool_args(self, request):
        params = request.get("params", {})
        name, args = params.get("name"), params.get("arguments")
        tool = next((tool for tool in self.tools if tool["name"] == name), None)
        if not tool or not isinstance(args, dict):
            raise InvalidToolArguments("Unknown MindIE tool or invalid arguments")
        args = dict(args)
        schema = tool["inputSchema"]
        unknown = set(args) - set(schema["properties"])
        missing = set(schema["required"]) - set(args)
        if unknown or missing:
            detail = "; ".join(part for part in [
                "unknown keys: " + ", ".join(sorted(unknown)) if unknown else "",
                "missing keys: " + ", ".join(sorted(missing)) if missing else "",
            ] if part)
            raise InvalidToolArguments("Invalid MindIE tool arguments (" + detail + ")")
        return name, args

    def _request_identity(self, request):
        return hashlib.sha256(
            json.dumps(request.get("id"), sort_keys=True).encode()
        ).hexdigest()

    def _remote_call(self, request, cancel, timeout, cli_identity):
        admitted = False
        outcome = "unknown"
        receipts = None
        stage = "admission"
        started = time.monotonic()
        name = None
        result = None
        try:
            if cli_identity is None:
                session = native_identity(request)
            else:
                # Trusted local plumbing for domain CLIs: env-derived identity
                # supplied out of band. Remote never checks a knowledge
                # activation bearer; the token element is ignored by design.
                session = cli_identity[0]
                if not isinstance(session, str) or not IDENTITY.fullmatch(session):
                    raise ValueError("Valid native task identity required")
            name, args = self._tool_args(request)
            receipts = self._remote_receipts.get(session)
            if receipts is None:
                receipts = RemoteReceipts(session)
                self._remote_receipts[session] = receipts
            if cli_identity is None:
                turn = request["params"]["_meta"]["x-codex-turn-metadata"].get("turn_id")
                if not isinstance(turn, str) or not IDENTITY.fullmatch(turn):
                    raise ValueError("Native turn identity required for remote request receipt")
                # connection_id is this Gate, and one Gate serves one stdio
                # process. Duplicate detection is connection-local: a numeric
                # RPC id reused after reconnect is not the same invocation.
                # An uncertain remote command is not automatically replayed.
                identity = (
                    turn + ":" + self.connection_id + ":"
                    + self._request_identity(request)
                )
            else:
                # Domain CLI has no host turn id. Its existing nonce is this
                # connection; do not add a second one.
                turn = "cli-" + self.connection_id
                identity = turn + ":" + self._request_identity(request)
            if not receipts.claim(identity):
                raise ValueError("Duplicate MCP request; not executed again")
            admitted = True
            payload = dict(
                surface="remote",
                name=name,
                arguments=args,
                remote_session_id=session,
            )
            bound = timeout
            with update_lock(config_path()):
                config = _adapter_config()
                stage = "helper_run"
                output = run(
                    [
                        config["python"],
                        str(Path(runtime_scripts(config)) / "runtime_call.py"),
                    ],
                    json.dumps(payload),
                    timeout=bound,
                    cancel=cancel,
                    env=runtime_env(config_path()),
                )
            stage = "helper_response"
            result = runtime_result(output)
            details = result.get("structuredContent")
            error_details = details.get("error_details") if isinstance(details, dict) else None
            uncertain = isinstance(details, dict) and (
                details.get("status") == "submission_uncertain"
                or isinstance(error_details, dict) and error_details.get("submission_state") == "uncertain")
            outcome = "unknown" if uncertain else result.get("operation_outcome") or ("failed" if result.get("isError") is True else "succeeded")
            return result
        except Exception as exc:
            # No traceback, credentials, model wakeup, reconnect loop or replay.
            result = call_failure(exc)
            if stage in ("helper_run", "helper_response") and not (
                cancel is not None and cancel.is_set()
            ):
                if stage == "helper_response":
                    category = "helper_protocol"
                elif isinstance(exc, TimeoutError):
                    category = "helper_timeout"
                else:
                    category = "helper_failed"
                diagnostic = diagnostic_failure(
                    "mcp." + self.surface + "." + name,
                    stage,
                    category,
                    exception=exc,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                result = helper_failure(result, diagnostic, stage, category)
            return result
        finally:
            if admitted and receipts is not None:
                try:
                    receipts.finish(identity, outcome)
                except Exception as accounting_error:
                    if isinstance(result, dict):
                        warning = dict(status="failed", error_type=type(accounting_error).__name__,
                                       operation_outcome="preserved", automatic_retry=False)
                        result["accounting"] = warning
                        result["local_accounting_warning"] = warning
                        if not isinstance(result.get("content"), list):
                            result["content"] = []
                        result["content"].append(dict(type="text", text=
                            "Remote result preserved; local outcome accounting failed. Do not repeat the operation."))

    def _call(self, request, session, cancel=None, timeout=None):
        token = None
        stage = "admission"
        started = time.monotonic()
        name = None
        try:
            name, args = self._tool_args(request)
            config = json.loads(self.sessions.config.read_text(encoding='utf-8'))
            payload = dict(
                surface=self.surface,
                name=name,
                arguments=args,
                mindie_session_id=session,
                native_session_verified=True,
            )
            bound = timeout
            try:
                # Exactly one installed runtime: the recorded interpreter
                # plus the recorded scripts directory, read in one config load
                # while the operation lock is held by Gate.call.
                stage = "helper_run"
                output = run(
                    [
                        config["python"],
                        str(Path(runtime_scripts(config)) / "runtime_call.py"),
                    ],
                    json.dumps(payload),
                    timeout=bound,
                    cancel=cancel,
                    env=runtime_env(self.sessions.config),
                    max_output=KNOWLEDGE_MAX_OUTPUT,
                    allow_service=True,
                )
            except Exception as exc:
                # A timeout or refused connection is availability, not a
                # paused grant. Protocol and configuration failures still count.
                if token is not None and not (
                    isinstance(exc, (TimeoutError, ConnectionError))
                    or type(exc).__name__ in {"URLError", "TimeoutExpired"}
                ):
                    try:
                        self.sessions.finish(session, token, False)
                    except Exception:
                        pass
                raise
            stage = "helper_response"
            result = runtime_result(output)
            return result
        except Exception as exc:
            # No traceback, credentials, model wakeup, reconnect loop or replay.
            result = call_failure(exc)
            if stage in ("helper_run", "helper_response") and not (
                cancel is not None and cancel.is_set()
            ):
                if stage == "helper_response":
                    category = "helper_protocol"
                elif isinstance(exc, TimeoutError):
                    category = "helper_timeout"
                else:
                    category = "helper_failed"
                diagnostic = diagnostic_failure(
                    "mcp." + self.surface + "." + name,
                    stage,
                    category,
                    exception=exc,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                result = helper_failure(result, diagnostic, stage, category)
            return result


def serve(surface):
    from importlib.metadata import version
    adapter_version = version("mindie-agent-codex")
    gate = Gate(surface)
    output_lock, pending_lock = threading.Lock(), threading.Lock()
    pending = {}
    capacity = threading.BoundedSemaphore(4)
    executor = ThreadPoolExecutor(max_workers=4)
    from update_trigger import Maintenance
    maintenance = Maintenance(gate.config)
    maintenance.trigger()

    def send(value):
        with output_lock:
            sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()

    def respond(identifier, result):
        send(dict(jsonrpc="2.0", id=identifier, result=result))

    def execute(message, cancel):
        try:
            from diagnostic_support import attach_pending, acknowledge_pending
            result = attach_pending(gate.call(message, cancel))
            respond(message["id"], result)
            acknowledge_pending(result)
        finally:
            with pending_lock:
                pending.pop(message["id"], None)
            capacity.release()

    try:
        while True:
            raw = sys.stdin.buffer.readline()
            if not raw:
                break
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("JSON-RPC object required")
                method, identifier = message.get("method"), message.get("id")
                if method == "notifications/cancelled":
                    with pending_lock:
                        call = pending.get(
                            (message.get("params") or {}).get("requestId")
                        )
                        if call:
                            call[0].set()
                    continue
                if identifier is None:
                    continue
                if type(identifier) not in (str, int) or (
                    isinstance(identifier, str) and len(identifier) > 256
                ):
                    raise ValueError("Invalid request identity")
                if method == "initialize":
                    respond(
                        identifier,
                        dict(
                            protocolVersion="2025-11-25",
                            capabilities={"tools": {}},
                            serverInfo=dict(name="mindie-" + surface, version=adapter_version),
                        ),
                    )
                elif method == "ping":
                    respond(identifier, {})
                elif method == "tools/list":
                    respond(identifier, dict(tools=gate.tools))
                elif method == "tools/call":
                    if (message.get("params") or {}).get("name") == INTERRUPT_TOOL:
                        try:
                            with pending_lock:
                                if identifier in pending:
                                    raise ValueError("Duplicate native interrupt request identity")
                                result = interrupt_turn(message, pending)
                        except ValueError as exc:
                            result = call_failure(exc)
                        respond(identifier, result)
                        continue
                    try:
                        identity = native_turn_identity(message)
                    except ValueError as exc:
                        respond(identifier, call_failure(exc))
                        continue
                    maintenance.trigger()
                    with pending_lock:
                        if (
                            identifier in pending
                            or not capacity.acquire(blocking=False)
                        ):
                            respond(
                                identifier,
                                failure(
                                    "MCP duplicate/capacity limit; not executed, do not retry automatically"
                                ),
                            )
                            continue
                        event = threading.Event()
                        pending[identifier] = event, identity
                    executor.submit(execute, message, event)
                else:
                    send(
                        dict(
                            jsonrpc="2.0",
                            id=identifier,
                            error=dict(code=-32601, message="Unsupported method"),
                        )
                    )
            except (ValueError, TypeError, AttributeError):
                send(
                    dict(
                        jsonrpc="2.0",
                        id=None,
                        error=dict(code=-32600, message="Invalid MCP request"),
                    )
                )
    finally:
        maintenance.close()
        with pending_lock:
            for event, _ in pending.values():
                event.set()
        executor.shutdown(wait=True)
