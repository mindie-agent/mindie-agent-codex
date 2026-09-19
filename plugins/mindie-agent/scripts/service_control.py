"""Explicit operator inspection/shutdown; never ensure_service()."""

import json
import sys
from session_gate import config_path

if __name__ == "__main__":
    if sys.argv[1:] not in (["status"], ["shutdown"]):
        raise SystemExit(1)
    from mindie_knowledge.loop.cli import config_at, connect
    from mindie_knowledge.loop.transport import rpc

    config = json.loads(config_path().read_text())
    result = rpc(
        connect(config_at(config["engine_config"])),
        "status" if sys.argv[1] == "status" else "stop",
        timeout=2,
    )
    print(json.dumps(result, ensure_ascii=False))
