#!/usr/bin/env python3
"""Discover and serve remote tools without activating knowledge capture.

[Role]
Entrypoint wrapper that delegates stdio MCP serving to `mcp_gate.serve("remote")`.

[Invariants]
- Never activates knowledge capture or session leases on mere tool discovery.

[Timeouts & Locks]
- Inherits MCP gate request budgets and SQLite receipt timeouts.

[Side-Effect Policy]
- Rejects unsupported CLI arguments; delegates all runtime behavior to `mcp_gate.py`.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_gate import serve

if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("unsupported remote-dev entry operation")
    serve("remote")
