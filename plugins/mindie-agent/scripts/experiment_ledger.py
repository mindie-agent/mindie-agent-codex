#!/usr/bin/env python3
"""Codex CLI and module forwarder for `mindie_harness.rsi.experiment_ledger`.

[Role] Delegates RSI ledger operations (`fingerprint`, `gate`, `judge`, `append`, `fold`) to `mindie_harness.rsi`.
[Invariants] Falls back cleanly to `mindie_knowledge.graded.experiment_ledger` on pre-harness runtimes.
[Timeouts & Locks] Delegated to `mindie_harness.rsi.experiment_ledger` (`15.0s` Git, `5.0s` lock).
[Side-Effect Policy] Delegated to `mindie_harness.rsi.experiment_ledger`.
"""
from __future__ import annotations

import sys

try:
    from mindie_harness.rsi import experiment_ledger as _shared
except ModuleNotFoundError:
    from mindie_knowledge.graded import experiment_ledger as _shared

if __name__ == "__main__":
    raise SystemExit(_shared.main())

_alias = __name__
globals().update({k: v for k, v in _shared.__dict__.items() if k not in ("__name__", "__file__", "__spec__", "__loader__")})
sys.modules[_alias] = _shared
