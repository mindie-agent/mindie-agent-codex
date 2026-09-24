"""Static discovery plus host-identity-bound, bounded, one-shot runtime calls.

The remote surface is a general tool: every native Codex task may use it on
demand without MindIE activation, leases, or the knowledge engine. Each call
is bound to its native task from verified host metadata, and per-task
ownership reaches remote-dev's REMOTE_DEV_SESSION_ID so one task cannot
operate on another task's remote jobs. The knowledge surface is unchanged:
it still requires manual activation and a checked lease.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import sqlite3
import time
import threading
import uuid

from bounded_process import run
from diagnostic_support import attach as attach_diagnostic
from diagnostic_support import failure as diagnostic_failure
from session_gate import IDENTITY, Sessions, config_path, generation_env, runtime_scripts
from update_lock import update_lock

MAX_INPUT = 128 * 1024
KNOWLEDGE_TIMEOUT = 15
REMOTE_TIMEOUT = 65
CATALOG = Path(__file__).with_name("mcp_catalog.json")

REMOTE_MAX_FAILURES = 3


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
    return failure(f"{type(exc).__name__}: {str(exc)[:240]}. No automatic retry.")


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
    circuit and job-ownership bookkeeping.
    """
    override = os.environ.get("MINDIE_REMOTE_STATE_DIR")
    if override:
        return Path(override).expanduser().absolute()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base).expanduser().absolute() / "mindie-remote-dev"


def native_identity(request):
    """Bind one tools/call to its native task via verified host metadata.

    Codex delivers tools/call params._meta['x-codex-turn-metadata'] with
    thread_id/session_id/turn_id, and params._meta.threadId agreeing (root
    probe, task 01a0bcfa-cd11-7dc1-bfb4-bcd5ee180fd4, Codex 0.153.4). Every
    call is bound from this metadata; tool arguments never override it.
    Missing or contradictory metadata fails closed with a clear diagnostic
    for older hosts — the most recently activated lease is never a fallback.
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
    if not (thread == session == plain):
        raise ValueError(
            "Contradictory native task identity metadata; call rejected"
        )
    return thread


class RemoteReceipts:
    """Durable task-local admission, without a knowledge lease or service.

    SQLite rejects corrupt state instead of reopening an empty replay ledger.
    Request keys remain on disk, never in an unbounded in-memory collection;
    there is no lifetime call ceiling or eviction that makes old keys reusable.
    """

    def __init__(self, session):
        self.path = remote_state_dir() / "gate" / (session + ".sqlite3")

    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=0.2)
        os.chmod(self.path, 0o600)
        db.execute("PRAGMA cache_size=-2048")
        db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, failures INTEGER NOT NULL, paused INTEGER NOT NULL)")
        db.execute("INSERT OR IGNORE INTO state VALUES(1, 0, 0)")
        db.execute("CREATE TABLE IF NOT EXISTS attempts (identity TEXT PRIMARY KEY, started REAL NOT NULL, status TEXT NOT NULL)")
        db.commit()
        return db

    def claim(self, identity):
        db = self._db()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                # A killed gate leaves an uncertain, consumed receipt. It can
                # contribute to the circuit but can never become dispatchable.
                expired = db.execute("UPDATE attempts SET status='failed' WHERE status='running' AND started<?", (time.time() - REMOTE_TIMEOUT - 5,)).rowcount
                if expired:
                    db.execute("UPDATE state SET failures=failures+? WHERE id=1", (expired,))
                failures, paused = db.execute("SELECT failures, paused FROM state WHERE id=1").fetchone()
                if paused or failures >= REMOTE_MAX_FAILURES:
                    db.execute("UPDATE state SET paused=1 WHERE id=1")
                    db.commit()
                    raise ValueError("Remote calls paused after repeated failures; explicit recovery: remote_bridge.py recover in this native task")
                if db.execute("SELECT 1 FROM attempts WHERE identity=?", (identity,)).fetchone():
                    return False
                if db.execute("SELECT count(*) FROM attempts WHERE status='running'").fetchone()[0] >= 4:
                    raise ValueError("Remote task concurrency limit reached; no automatic retry")
                db.execute("INSERT INTO attempts VALUES(?, ?, 'running')", (identity, time.time()))
                return True
        finally:
            db.close()

    def finish(self, identity, succeeded):
        db = self._db()
        try:
            with db:
                db.execute("UPDATE attempts SET status=? WHERE identity=? AND status='running'", ("succeeded" if succeeded else "failed", identity))
                if succeeded:
                    db.execute("UPDATE state SET failures=0 WHERE id=1")
                else:
                    db.execute("UPDATE state SET failures=failures+1, paused=CASE WHEN failures+1>=? THEN 1 ELSE paused END WHERE id=1", (REMOTE_MAX_FAILURES,))
        finally:
            db.close()

    def recover(self):
        db = self._db()
        try:
            with db:
                # Explicit recovery releases only the circuit, never attempts.
                db.execute("UPDATE state SET failures=0, paused=0 WHERE id=1")
        finally:
            db.close()


def _adapter_config():
    path = config_path()
    if not path.is_file():
        raise ValueError(
            "MindIE adapter configuration is required to locate the runtime "
            "interpreter; remote execution fails closed"
        )
    return json.loads(path.read_text())


class Gate:
    def __init__(self, surface):
        self.surface = surface
        self.sessions = Sessions() if surface == "knowledge" else None
        self.tools = json.loads(CATALOG.read_text())[surface]
        self.connection_id = uuid.uuid4().hex

    def call(self, request, cancel=None, *, timeout=None, cli_identity=None):
        if self.surface == "remote":
            # General remote path: no MindIE activation, lease, knowledge
            # service or capture state is consulted or created.
            return self._remote_call(request, cancel, timeout, cli_identity)
        # Inactive discovery/calls must not create any local state.
        # Domain CLI reuses this path: missing config, missing lease, and the
        # bounded runtime all fail closed. There is no ungated development
        # bypass around claim/finish or the 65s remote budget.
        try:
            if not config_path().is_file():
                raise ValueError(
                    "MindIE adapter configuration is required; remote execution fails closed"
                )
            if cli_identity is None:
                session = native_identity(request)
                # Exactly this native task's lease; identity never comes from
                # arguments, and never from the most recently active lease.
                self.sessions.check(session)
            else:
                # Domain CLI path: no host metadata exists outside MCP, so the
                # explicit env-supplied activation is checked directly.
                session, token = cli_identity
                self.sessions.check(session, token)
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
        succeeded = False
        receipts = None
        stage = "admission"
        started = time.monotonic()
        name = None
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
            receipts = RemoteReceipts(session)
            if cli_identity is None:
                turn = request["params"]["_meta"]["x-codex-turn-metadata"].get("turn_id")
                if not isinstance(turn, str) or not IDENTITY.fullmatch(turn):
                    raise ValueError("Native turn identity required for remote request receipt")
            else:
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
            bound = REMOTE_TIMEOUT
            if timeout is not None:
                bound = min(max(0.01, float(timeout)), bound)
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
            result = json.loads(output)
            if not isinstance(result, dict) or not isinstance(
                result.get("content"), list
            ):
                raise ValueError("Invalid MindIE runtime response")
            succeeded = result.get("isError") is not True
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
                    receipts.finish(identity, succeeded)
                except Exception:
                    pass

    def _call(self, request, session, cancel=None, timeout=None):
        token = None
        stage = "admission"
        started = time.monotonic()
        name = None
        try:
            name, args = self._tool_args(request)
            lease = self.sessions.check(session)
            token = lease["token"]
            if not self.sessions.claim(
                session, "mcp", self.connection_id + ":" + self._request_identity(request), token
            ):
                raise ValueError("Duplicate MCP request; not executed again")
            config = json.loads(self.sessions.config.read_text())
            payload = dict(
                surface=self.surface,
                name=name,
                arguments=args,
                mindie_session_id=session,
                mindie_activation=token,
            )
            bound = (
                KNOWLEDGE_TIMEOUT if self.surface == "knowledge" else REMOTE_TIMEOUT
            )
            if timeout is not None:
                bound = min(max(0.01, float(timeout)), bound)
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
                )
            except Exception as exc:
                # A timeout or refused connection is availability, not a
                # paused grant. Protocol and configuration failures still count.
                if not (
                    isinstance(exc, (TimeoutError, ConnectionError))
                    or type(exc).__name__ in {"URLError", "TimeoutExpired"}
                ):
                    try:
                        self.sessions.finish(session, token, False)
                    except Exception:
                        pass
                raise
            stage = "helper_response"
            result = json.loads(output)
            if not isinstance(result, dict) or not isinstance(
                result.get("content"), list
            ):
                raise ValueError("Invalid MindIE runtime response")
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
    pending, seen = {}, set()
    capacity = threading.BoundedSemaphore(4)
    executor = ThreadPoolExecutor(max_workers=4)

    def send(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=False), flush=True)

    def respond(identifier, result):
        send(dict(jsonrpc="2.0", id=identifier, result=result))

    def execute(message, cancel):
        try:
            respond(message["id"], gate.call(message, cancel))
        finally:
            with pending_lock:
                pending.pop(message["id"], None)
            capacity.release()

    try:
        while True:
            raw = sys.stdin.buffer.readline(MAX_INPUT + 1)
            if not raw:
                break
            if len(raw) > MAX_INPUT:
                # Close this transport instead of reading/allocating an unbounded frame.
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
                            (identifier in seen if surface != "remote" else identifier in pending)
                            or (surface != "remote" and len(seen) >= 4096)
                            or not capacity.acquire(blocking=False)
                        ):
                            respond(
                                identifier,
                                failure(
                                    "MCP duplicate/capacity limit; not executed, do not retry automatically"
                                ),
                            )
                            continue
                        if surface != "remote":
                            seen.add(identifier)
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
                        error=dict(code=-32600, message="Invalid bounded MCP request"),
                    )
                )
    finally:
        with pending_lock:
            for event in pending.values():
                event.set()
        executor.shutdown(wait=True)
