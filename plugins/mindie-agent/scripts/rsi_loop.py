#!/usr/bin/env python3
"""Codex CLI and module forwarder for `mindie_harness.rsi.rsi_loop`.

[Role] Delegates `init`, `next_step`, `complete`, and `finish` to `mindie_harness.rsi`.
[Invariants] Falls back cleanly to `mindie_knowledge.graded.rsi_loop` on pre-harness runtimes.
[Timeouts & Locks] Delegated to `mindie_harness.rsi.rsi_loop` (`5.0s` exclusive lock).
[Side-Effect Policy] Delegated to `mindie_harness.rsi.rsi_loop`.
"""
from __future__ import annotations

import sys

try:
    from mindie_harness.rsi import rsi_loop as _shared
except ModuleNotFoundError:
    from mindie_knowledge.graded import rsi_loop as _shared

if __name__ == "__main__":
    raise SystemExit(_shared.main())

_alias = __name__
globals().update({k: v for k, v in _shared.__dict__.items() if k not in ("__name__", "__file__", "__spec__", "__loader__")})
sys.modules[_alias] = _shared
