#!/usr/bin/env python3
"""Discover remote tools without importing or starting the remote runtime."""

import sys
from mcp_gate import serve

if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("unsupported remote-dev entry operation")
    serve("remote")
