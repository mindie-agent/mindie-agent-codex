"""Static discovery plus host-identity-bound, bounded, one-shot runtime calls.

The remote surface is a general tool: every native Codex task may use it on
demand without the MindIE entry, leases, or the knowledge engine. Each call
is bound to its native task from verified host metadata, and per-task
ownership reaches remote-dev's REMOTE_DEV_SESSION_ID so one task cannot
operate on another task's remote jobs. The knowledge surface requires a
bound task and a checked lease.
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
from session_gate import IDENTITY, Sessions, config_path, generation_env, runtime_scripts
from update_lock import update_lock, file_lock

# Knowledge stdout only. A legal maximum page measured 817407 bytes.
KNOWLEDGE_MAX_OUTPUT = 1024 * 1024
CATALOG = Path(__file__).with_name("mcp_catalog.json")


def failure(message):
    return dict(content=[dict(type="text", text=message)], isError=True)


class InvalidToolArguments(ValueError):
    """Rejected by the declared tool schema before any runtime dispatch."""


def call_failure(exc):
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


def runtime_result(completed):
    """A helper's valid business envelope survives its separate cleanup fault."""
    if not isinstance(completed, ProcessResult) or completed.execution != "completed":
        raise ValueError("runtime helper has no completed process result")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        raise ValueError("Invalid MindIE runtime response")
    if completed.cleanup:
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


class RemoteReceipts:
    """Durable task-local admission, without a knowledge lease or service.

    SQLite rejects corrupt state instead of reopening an empty replay ledger.
    Request keys remain on disk, never in an unbounded in-memory collection;
    there is no lifetime call ceiling or eviction that makes old keys reusable.
    A failed operation never prevents a later independent diagnostic call.
    Uncertain effects remain consumed receipts and cannot be replayed.
    """

    def __init__(self, session):
        self.path = remote_state_dir() / "gate" / (session + ".sqlite3")
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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = self.path.with_suffix(".authority.json")
        with file_lock(self.path.with_suffix(".initialize.lock"), exclusive=True, blocking=True):
            try:
                authority = json.loads(marker.read_text(encoding="utf-8"))
            except FileNotFoundError:
                if self.path.exists():
                    raise RuntimeError("existing remote receipt database lacks its authority marker") from None
                authority = dict(schema="mindie-remote-receipts/1", identity=uuid.uuid4().hex)
                # First-use admission is durable before SQLite creation. A
                # partial creation remains an error on the next call.
                with marker.open("x", encoding="utf-8") as stream:
                    os.chmod(marker, 0o600)
                    json.dump(authority, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                db = sqlite3.connect(self.path, timeout=0.2)
                try:
                    os.chmod(self.path, 0o600)
                    with db:
                        db.execute("CREATE TABLE authority(identity TEXT PRIMARY KEY, schema TEXT NOT NULL)")
                        db.execute("INSERT INTO authority VALUES(?,?)", (authority["identity"], authority["schema"]))
                        db.execute("CREATE TABLE attempts (identity TEXT PRIMARY KEY, started REAL NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL)")
                        db.execute("CREATE INDEX attempts_running_owner ON attempts(owner) WHERE status='running'")
                finally:
                    db.close()
            if (not isinstance(authority, dict) or authority.get("schema") != "mindie-remote-receipts/1"
                    or not isinstance(authority.get("identity"), str) or len(authority["identity"]) != 32):
                raise ValueError("remote receipt authority marker is invalid")
            db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=0.2)
            try:
                if db.execute("SELECT identity,schema FROM authority").fetchall() != [(authority["identity"], authority["schema"])]:
                    raise ValueError("remote receipt authority identity differs")
                if [row[1] for row in db.execute("PRAGMA table_info(attempts)")] != ["identity", "started", "status", "owner"]:
                    raise ValueError("remote receipt schema is incomplete")
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='attempts_running_owner'").fetchone():
                    raise ValueError("remote receipt live-owner index is missing")
                db.execute("PRAGMA cache_size=-2048")
                return db
            except BaseException:
                db.close()
                raise

    def claim(self, identity):
        self._ensure_owner()
        db = self._db()
        try:
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
    if not path.is_file():
        raise ValueError(
            "MindIE adapter configuration is required to locate the runtime "
            "interpreter; remote execution fails closed"
        )
    return json.loads(path.read_text(encoding='utf-8'))


class Gate:
    def __init__(self, surface):
        self.surface = surface
        self.sessions = Sessions() if surface == "knowledge" else None
        self.tools = json.loads(CATALOG.read_text(encoding='utf-8'))[surface]
        self.connection_id = uuid.uuid4().hex
        self._remote_receipts = {}

    def call(self, request, cancel=None, *, timeout=None, cli_identity=None):
        if self.surface == "remote":
            # General remote path: no MindIE activation, lease, knowledge
            # service or capture state is consulted or created.
            return self._remote_call(request, cancel, timeout, cli_identity)
        # Discovery does not create capture state. The native or trusted CLI
        # boundary verifies task identity; ordinary reads need no capture lease.
        # Runtime failures remain visible without an implicit execution TTL.
        try:
            if not config_path().is_file():
                raise ValueError(
                    "MindIE adapter configuration is required; remote execution fails closed"
                )
            if cli_identity is None:
                session = native_identity(request)
                # Identity never comes from arguments or another task's lease.
            else:
                # Domain CLI path: no host metadata exists outside MCP, so the
                # explicit environment-supplied identity is checked directly.
                session, token = cli_identity
            with update_lock(self.sessions.config):
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
                    env=generation_env(config_path()),
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
                        result["accounting"] = dict(status="failed", error_type=type(accounting_error).__name__,
                                                    operation_outcome="preserved", automatic_retry=False)
                        result["isError"] = True
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
                # Exactly one committed generation: the recorded interpreter
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
                    env=generation_env(self.sessions.config),
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
    gate = Gate(surface)
    output_lock, pending_lock = threading.Lock(), threading.Lock()
    pending = {}
    capacity = threading.BoundedSemaphore(4)
    executor = ThreadPoolExecutor(max_workers=4)

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
                        event = pending.get(
                            (message.get("params") or {}).get("requestId")
                        )
                        if event:
                            event.set()
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
                            serverInfo=dict(name="mindie-" + surface, version="0.2.0"),
                        ),
                    )
                elif method == "ping":
                    respond(identifier, {})
                elif method == "tools/list":
                    respond(identifier, dict(tools=gate.tools))
                elif method == "tools/call":
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
                        pending[identifier] = event
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
        with pending_lock:
            for event in pending.values():
                event.set()
        executor.shutdown(wait=True)
