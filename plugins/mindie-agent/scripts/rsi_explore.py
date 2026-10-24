"""RSI explore-mode MCP argument shaping for `knowledge_query` / `knowledge_explain`.

[Role]
Declares the optional `mode`, `ledger_entries`, `bottleneck_summary`,
`native_subcontext` and `goal` MCP properties and normalizes them at this
host's protocol boundary before the call reaches the knowledge service.

[Invariants]
- `mode` is exactly `reuse`, `diagnose` or `explore`; an unknown mode and a
  malformed `ledger_entries` / `bottleneck_summary` are rejected here, before
  any lease, service start or store operation.
- Normalization fills the optional keys with their declared defaults so the
  service always receives one explicit shape; it never invents a mode.
- Explore semantics belong to the knowledge engine: it strips L3 workflows,
  promotes their L1/L2 foundations, and returns `exploration_context` with its
  `mindie-rsi-subagent-pack/1` fact pack. This adapter never rebuilds, filters
  or renormalizes that engine-owned result.

[Timeouts & Locks]
- Pure in-memory argument shaping; no I/O, model call, subprocess, sleep or
  lock is introduced.

[Side-Effect Policy]
- Returns a normalized copy of the caller's arguments; writes nothing.
"""

from __future__ import annotations

MODES = frozenset({"reuse", "diagnose", "explore"})
OPTIONAL_KEYS = (
    "ledger_entries",
    "bottleneck_summary",
    "native_subcontext",
    "goal",
)


def schema_properties() -> dict:
    """The exact optional MCP properties shared by query and explain."""
    return {
        "mode": {"type": "string", "enum": ["reuse", "diagnose", "explore"]},
        "ledger_entries": {"type": "array", "items": {"type": "object"}},
        "bottleneck_summary": {"type": "object"},
        "native_subcontext": {"type": "boolean"},
        "goal": {"type": "string"},
    }


def parse_args(arguments) -> dict:
    """Validate RSI arguments and return a normalized copy of the call."""
    body = dict(arguments or {})
    mode = str(body.get("mode", "reuse") or "reuse")
    if mode not in MODES:
        raise ValueError("mode must be one of reuse, diagnose or explore")
    ledger = body.get("ledger_entries")
    if ledger is not None and (
        not isinstance(ledger, list)
        or any(not isinstance(item, dict) for item in ledger)
    ):
        raise ValueError("ledger_entries must be an array of objects")
    bottleneck = body.get("bottleneck_summary")
    if bottleneck is not None and not isinstance(bottleneck, dict):
        raise ValueError("bottleneck_summary must be an object")
    body["mode"] = mode
    body["ledger_entries"] = ledger
    body["bottleneck_summary"] = bottleneck
    body["native_subcontext"] = bool(body.get("native_subcontext", False))
    body["goal"] = str(body.get("goal", "") or "")
    return body


def forwarded(arguments) -> dict:
    """Return arguments plus RSI keys only when the caller selected them."""
    body = dict(arguments or {})
    selected = body.get("mode") != "reuse" or any(
        key in body for key in ("mode",) + OPTIONAL_KEYS
    )
    if selected:
        body = parse_args(body)
    return body


__all__ = ["MODES", "OPTIONAL_KEYS", "forwarded", "parse_args", "schema_properties"]
