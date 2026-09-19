"""Static discovery plus session-gated, bounded, one-shot runtime calls."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import threading
import uuid

from bounded_process import run
from session_gate import Sessions, config_path
from update_lock import update_lock

MAX_INPUT = 128 * 1024
KNOWLEDGE_TIMEOUT = 15
REMOTE_TIMEOUT = 65
CATALOG = Path(__file__).with_name("mcp_catalog.json")


def failure(message):
    return dict(content=[dict(type="text", text=message)], isError=True)


class Gate:
    def __init__(self, surface):
        self.surface = surface
        self.sessions = Sessions()
        self.tools = json.loads(CATALOG.read_text())[surface]
        self.connection_id = uuid.uuid4().hex

    def call(self, request, cancel=None, *, timeout=None):
        # Inactive discovery/calls must not create any local state.
        # Domain CLI reuses this path: missing config, missing lease, and the
        # bounded runtime all fail closed. There is no ungated development
        # bypass around claim/finish or the 65s remote budget.
        try:
            if not config_path().is_file():
                raise ValueError(
                    "MindIE adapter configuration is required; remote execution fails closed"
                )
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(
                params.get("arguments"), dict
            ):
                raise ValueError("Invalid MindIE tool arguments")
            args = params["arguments"]
            self.sessions.check(
                args.get("mindie_session_id"), args.get("mindie_activation", "")
            )
            with update_lock(self.sessions.config):
                return self._call(request, cancel, timeout=timeout)
        except Exception as exc:
            return failure(
                f"{type(exc).__name__}: {str(exc)[:240]}. No automatic retry."
            )

    def _call(self, request, cancel=None, timeout=None):
        session = token = None
        admitted = False
        succeeded = False
        try:
            params = request.get("params", {})
            name, args = params.get("name"), params.get("arguments")
            tool = next((tool for tool in self.tools if tool["name"] == name), None)
            if not tool or not isinstance(args, dict):
                raise ValueError("Unknown MindIE tool or invalid arguments")
            args = dict(args)
            session = args.pop("mindie_session_id", None)
            token = args.pop("mindie_activation", None)
            if not isinstance(token, str) or not token:
                raise ValueError(
                    "Manual MindIE session activation required; continue without the plugin"
                )
            self.sessions.check(session, token)
            schema = tool["inputSchema"]
            unknown = set(args) - set(schema["properties"])
            missing = set(schema["required"]) - {"mindie_session_id", "mindie_activation"} - set(args)
            if unknown or missing:
                detail = "; ".join(part for part in [
                    "unknown keys: " + ", ".join(sorted(unknown)) if unknown else "",
                    "missing keys: " + ", ".join(sorted(missing)) if missing else "",
                ] if part)
                raise ValueError("Invalid MindIE tool arguments (" + detail + ")")
            if (
                self.surface == "knowledge"
                and "session_id" in args
                and args["session_id"] != session
            ):
                raise ValueError(
                    "Knowledge session does not match the manual activation"
                )
            identity = hashlib.sha256(
                json.dumps(request.get("id"), sort_keys=True).encode()
            ).hexdigest()
            if not self.sessions.claim(
                session, "mcp", self.connection_id + ":" + identity, token
            ):
                raise ValueError("Duplicate MCP request; not executed again")
            admitted = True
            config = json.loads(config_path().read_text())
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
            output = run(
                [config["python"], str(Path(__file__).with_name("runtime_call.py"))],
                json.dumps(payload),
                timeout=bound,
                cancel=cancel,
            )
            result = json.loads(output)
            if not isinstance(result, dict) or not isinstance(
                result.get("content"), list
            ):
                raise ValueError("Invalid MindIE runtime response")
            succeeded = result.get("isError") is not True
            return result
        except Exception as exc:
            # No traceback, credentials, model wakeup, reconnect loop or replay.
            return failure(
                f"{type(exc).__name__}: {str(exc)[:240]}. No automatic retry."
            )
        finally:
            if admitted:
                try:
                    self.sessions.finish(session, token, succeeded)
                except Exception:
                    pass


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
                            identifier in seen
                            or len(seen) >= 4096
                            or not capacity.acquire(blocking=False)
                        ):
                            respond(
                                identifier,
                                failure(
                                    "MCP duplicate/capacity limit; not executed, do not retry automatically"
                                ),
                            )
                            continue
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
