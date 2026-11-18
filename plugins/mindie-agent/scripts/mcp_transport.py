"""Native Codex MCP transport with live tool catalog invalidation.

[Role] Serve bounded requests without tying the connection to one runtime generation.
[Invariants] Keep native identities, cancellation and active requests intact on update;
read-only tools (`remote_read`, `remote_grep`, `readOnlyHint=True`) run concurrently
without consuming the 4-slot mutation semaphore (`AD-09`).
[Timeouts & Locks] 4 mutation slots + 24 read-only worker slots; serialized stdout;
1s selected-catalog watcher.
[Side-Effect Policy] Delegates calls to Gate; catalog refresh never activates knowledge;
on normal stdin EOF it releases RemoteReceipts owner sidecars.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
from remote_dev.mcp.catalog_refresh import CatalogRefresh
from live_catalog import revision

MAX_MUTATION_CONCURRENCY = 4
MAX_READ_ONLY_CONCURRENCY = 24


def _is_read_only_call(gate_api, gate, message):
    tool_name = (message.get("params") or {}).get("name")
    if not isinstance(tool_name, str) or not tool_name:
        return False
    checker = getattr(gate_api, "is_read_only_tool", None)
    # Read only the static in-memory `_tools` list; never evaluate `@property gate.tools`
    # on the transport reader thread where catalog subprocess timeouts could escape `serve()`.
    tools = gate._tools if hasattr(gate, "_tools") else ()
    if callable(checker):
        return bool(checker(tool_name, tools))
    for tool in tools or ():
        if isinstance(tool, dict) and tool.get("name") == tool_name:
            annotations = tool.get("annotations")
            if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
                return True
    return False


def serve(surface, gate_api):
    from importlib.metadata import version
    adapter_version = version("mindie-agent-codex")
    gate = gate_api.Gate(surface)
    output_lock, pending_lock = threading.Lock(), threading.Lock()
    pending = {}
    mutation_capacity = threading.BoundedSemaphore(MAX_MUTATION_CONCURRENCY)
    read_capacity = threading.BoundedSemaphore(MAX_READ_ONLY_CONCURRENCY)
    executor = ThreadPoolExecutor(
        max_workers=MAX_MUTATION_CONCURRENCY + MAX_READ_ONLY_CONCURRENCY
    )
    from update_trigger import Maintenance
    maintenance = Maintenance(gate.config)
    maintenance.trigger()

    def send(value):
        with output_lock:
            sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()

    refresh = CatalogRefresh(lambda: revision(gate.config), send)

    def respond(identifier, result):
        send(dict(jsonrpc="2.0", id=identifier, result=result))

    def execute(message, cancel, sem):
        try:
            from diagnostic_support import attach_pending, acknowledge_pending
            result = attach_pending(gate.call(message, cancel))
            refresh.check()
            respond(message["id"], result)
            acknowledge_pending(result)
        finally:
            with pending_lock:
                pending.pop(message["id"], None)
            sem.release()

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
                            capabilities={"tools": {"listChanged": True}},
                            serverInfo=dict(name="mindie-" + surface, version=adapter_version),
                        ),
                    )
                    refresh.start()
                elif method == "ping":
                    respond(identifier, {})
                elif method == "tools/list":
                    respond(identifier, dict(tools=gate.tools))
                elif method == "tools/call":
                    if (message.get("params") or {}).get("name") == gate_api.INTERRUPT_TOOL:
                        try:
                            with pending_lock:
                                if identifier in pending:
                                    raise ValueError("Duplicate native interrupt request identity")
                                result = gate_api.interrupt_turn(message, pending)
                        except ValueError as exc:
                            result = gate_api.call_failure(exc)
                        respond(identifier, result)
                        continue
                    try:
                        default_session = "mcp-" + getattr(gate, "connection_id", "default")
                        thread_id, turn_id = gate_api.optional_turn_identity(
                            message, default_session=default_session
                        )
                        identity = (thread_id, turn_id) if turn_id is not None else None
                    except ValueError as exc:
                        respond(identifier, gate_api.call_failure(exc))
                        continue
                    maintenance.trigger()
                    sem = (
                        read_capacity
                        if _is_read_only_call(gate_api, gate, message)
                        else mutation_capacity
                    )
                    with pending_lock:
                        if (
                            identifier in pending
                            or not sem.acquire(blocking=False)
                        ):
                            respond(
                                identifier,
                                gate_api.failure(
                                    "MCP duplicate/capacity limit; not executed, do not retry automatically"
                                ),
                            )
                            continue
                        event = threading.Event()
                        pending[identifier] = event, identity
                    executor.submit(execute, message, event, sem)
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
        refresh.close()
        maintenance.close()
        with pending_lock:
            for event, _ in pending.values():
                event.set()
        executor.shutdown(wait=True)
        closer = getattr(gate, "close", None)
        if callable(closer):
            closer()
