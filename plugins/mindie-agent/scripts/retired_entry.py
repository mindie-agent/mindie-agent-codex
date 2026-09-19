"""Keep obsolete cache paths callable without admitting business work."""

import json
import sys

MESSAGE = "This MindIE version is retired. Use a new task and manually invoke $mindie-agent. Do not retry automatically."

if len(sys.argv) > 1 and sys.argv[1] in {"stop", "session-start"}:
    print("{}")
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] != "mcp":
    print(MESSAGE, file=sys.stderr)
    raise SystemExit(1)
for raw in iter(lambda: sys.stdin.buffer.readline(131073), b""):
    if len(raw) > 131072:
        break
    try:
        message = json.loads(raw)
        if not isinstance(message, dict) or message.get("id") is None:
            continue
        method = message.get("method")
        if method == "initialize":
            result = dict(
                protocolVersion="2025-11-25",
                capabilities={"tools": {}},
                serverInfo=dict(name="mindie-retired", version="0"),
            )
        elif method == "tools/list":
            result = dict(tools=[])
        elif method == "ping":
            result = {}
        else:
            result = dict(content=[dict(type="text", text=MESSAGE)], isError=True)
        print(
            json.dumps(dict(jsonrpc="2.0", id=message["id"], result=result)), flush=True
        )
    except (ValueError, TypeError):
        print(
            json.dumps(
                dict(
                    jsonrpc="2.0",
                    id=None,
                    error=dict(code=-32600, message="Invalid request"),
                )
            ),
            flush=True,
        )
