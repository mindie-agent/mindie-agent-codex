#!/usr/bin/env python3
"""Forwarder to `mindie_harness.rsi.rsi_explore` with pre-harness fallback.

[Role] Delegates RSI explore-mode argument shaping to `mindie_harness.rsi`.
[Invariants] Rejects unknown `mode` and malformed `ledger_entries` / `bottleneck_summary`.
[Timeouts & Locks] Pure in-memory validation; zero I/O or locks.
[Side-Effect Policy] Returns a shallow normalized copy; mutates nothing.
"""
from __future__ import annotations

try:
    from mindie_harness.rsi.rsi_explore import MODES, OPTIONAL_KEYS, forwarded, parse_args, schema_properties
except ModuleNotFoundError:
    MODES = frozenset({"reuse", "diagnose", "explore"})
    OPTIONAL_KEYS = ("ledger_entries", "bottleneck_summary", "native_subcontext", "goal")
    def schema_properties() -> dict:
        return {"mode": {"type": "string", "enum": ["reuse", "diagnose", "explore"]}, "ledger_entries": {"type": "array", "items": {"type": "object"}}, "bottleneck_summary": {"type": "object"}, "native_subcontext": {"type": "boolean"}, "goal": {"type": "string"}}
    def parse_args(arguments) -> dict:
        b = dict(arguments or {}); m = str(b.get("mode", "reuse") or "reuse")
        if m not in MODES: raise ValueError("mode must be one of reuse, diagnose or explore")
        l, s = b.get("ledger_entries"), b.get("bottleneck_summary")
        if l is not None and (not isinstance(l, list) or any(not isinstance(x, dict) for x in l)): raise ValueError("ledger_entries must be an array of objects")
        if s is not None and not isinstance(s, dict): raise ValueError("bottleneck_summary must be an object")
        return {**b, "mode": m, "ledger_entries": l, "bottleneck_summary": s, "native_subcontext": bool(b.get("native_subcontext", False)), "goal": str(b.get("goal", "") or "")}
    forwarded = parse_args

__all__ = ["MODES", "OPTIONAL_KEYS", "forwarded", "parse_args", "schema_properties"]
