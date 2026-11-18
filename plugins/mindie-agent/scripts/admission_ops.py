"""Legacy pre-0.1.38 `admission_ops.py` shim for in-flight `v0.1.37` MCP servers.
[Role] Respond with `{"ok": true, "result": {}}` when an already-open `v0.1.37` `session_gate._op` probes optional bind accounting after `codex.json` activates `0.1.39+`.
[Invariants] Zero pre-call session-binding gates (`P2.7`).
[Timeouts & Locks] Pure stdlib JSON response; no locks or subprocesses.
[Side-Effect Policy] Read-only stdin drain and JSON stdout.
"""
from __future__ import annotations
import json, sys
def main() -> int:
    sys.stdin.buffer.read(65536)
    print(json.dumps({"ok": True, "result": {}}))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
