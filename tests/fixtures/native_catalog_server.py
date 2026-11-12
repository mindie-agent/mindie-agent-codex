"""Selected helper fixture behind the real MindIE native MCP facade."""
import json
import os
from pathlib import Path
import sys

scripts = Path(__file__).resolve().parent
generation = scripts.name
payload = json.loads(sys.stdin.read())
if payload["surface"] == "catalog":
    result = [{
        "name": "refresh_probe", "description": generation,
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    }]
else:
    assert payload["surface"] == "components"
    assert payload["name"] == "refresh_probe"
    assert payload["native_session_verified"] is True
    config = Path(os.environ["MINDIE_AGENT_CONFIG"])
    value = json.loads(config.read_text(encoding="utf-8"))
    value["runtime_scripts"] = str(scripts.parent / "after")
    temporary = config.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8", newline="\n")
    temporary.replace(config)
    with (scripts.parent / "calls.jsonl").open("a", encoding="utf-8", newline="\n") as log:
        log.write(json.dumps({"generation": generation, "name": payload["name"]}) + "\n")
    result = {"content": [{"type": "text", "text": "updated"}], "isError": False}
print(json.dumps(result), flush=True)
