"""Explicit operator inspection/shutdown/cancel; never ensure_service()."""

import json
import sys
from session_gate import config_path

OPERATIONS = {"status": "status", "shutdown": "stop", "sharing-cancel": "sharing_cancel"}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in OPERATIONS:
        raise SystemExit(1)
    from mindie_knowledge.loop.cli import config_at, connect
    from mindie_knowledge.loop.transport import rpc

    config = json.loads(config_path().read_text())
    result = rpc(
        connect(config_at(config["engine_config"])),
        OPERATIONS[sys.argv[1]],
        timeout=2,
    )
    print(json.dumps(result, ensure_ascii=False))
