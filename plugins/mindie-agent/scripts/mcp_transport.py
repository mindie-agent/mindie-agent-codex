"""Native Codex MCP transport with live tool catalog invalidation.

[Role] Serve bounded requests without tying the connection to one runtime generation.
[Invariants] Keep native identities, cancellation and active requests intact on update.
[Timeouts & Locks] Four workers and serialized stdout; 1s selected-catalog watcher.
[Side-Effect Policy] Delegates calls to Gate; catalog refresh never activates knowledge.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
from remote_dev.mcp.catalog_refresh import CatalogRefresh
from live_catalog import revision

def serve(surface, gate_api):
    from importlib.metadata import version
    adapter_version = version("mindie-agent-codex")
    gate = gate_api.Gate(surface)
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

    refresh = CatalogRefresh(lambda: revision(gate.config), send)

    def respond(identifier, result):
        send(dict(jsonrpc="2.0", id=identifier, result=result))

    def execute(message, cancel):
        try:
            from diagnostic_support import attach_pending, acknowledge_pending
            result = attach_pending(gate.call(message, cancel))
            refresh.check()
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
                            capabilities={"tools": {"listChanged": True}},
                            serverInfo=dict(name="mindie-" + surface, version=adapter_version),
                        ),
                    )
                    refresh.start()
                elif method == "ping":
                    respond(identifier, {})
                elif method == "tools/list":
                    # A shared MCP connection cannot publish one chat's private
                    # grant to all other chats. Manual entry uses knowledge_cli.
                    respond(identifier, dict(tools=[] if surface == "knowledge" else gate.tools))
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
                        identity = gate_api.native_turn_identity(message)
                    except ValueError as exc:
                        respond(identifier, gate_api.call_failure(exc))
                        continue
                    maintenance.trigger()
                    with pending_lock:
                        if (
                            identifier in pending
                            or not capacity.acquire(blocking=False)
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
        refresh.close()
        maintenance.close()
        with pending_lock:
            for event, _ in pending.values():
                event.set()
        executor.shutdown(wait=True)

