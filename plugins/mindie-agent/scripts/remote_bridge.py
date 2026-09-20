#!/usr/bin/env python3
"""Discover remote tools without importing or starting the remote runtime.

Remote is a general tool: serving it never activates MindIE, touches a
knowledge lease/service/capture, or creates any local state. The only other
entry operation is `recover`: explicit native-CLI recovery of this task's
paused remote failure circuit. It reads the native CODEX_THREAD_ID from the
environment and never accepts a model-selected foreign id as an argument.
"""

import json
import os
import sys

from mcp_gate import RemoteReceipts, serve
from session_gate import IDENTITY


def recover():
    session = os.environ.get("CODEX_THREAD_ID", "")
    if not IDENTITY.fullmatch(session):
        raise SystemExit("Native CODEX_THREAD_ID required for remote recovery")
    RemoteReceipts(session).recover()
    print(json.dumps(dict(status="recovered", session=session)))


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        serve("remote")
    elif argv == ["recover"]:
        recover()
    else:
        raise SystemExit("unsupported remote-dev entry operation")
