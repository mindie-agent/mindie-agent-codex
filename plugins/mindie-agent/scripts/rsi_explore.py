"""RSI explore-mode argument forwarding and native-subcontext fact packs.

[Role]
Normalizes the optional `mode`, `ledger_entries`, `bottleneck_summary`,
`native_subcontext` and `goal` arguments for `knowledge_query` and
`knowledge_explain`, and guarantees an `exploration_context` on explore reads.

[Invariants]
- A call without any RSI argument is byte-for-byte unchanged; optional keys are
  forwarded only when the caller supplied them (or selected a non-reuse mode).
- `mode` is exactly `reuse`, `diagnose` or `explore`; an unknown mode is
  rejected before any service or store operation.
- Explore results withhold L3 workflow units while retaining mounted L1/L2/L4
  foundations; a fresh native subcontext is seeded only from the pack brief.
- A pre-existing `exploration_context` from a newer knowledge runtime is
  preserved and only supplemented when its pack is missing.

[Timeouts & Locks]
- Pure in-memory argument shaping and fact-pack assembly; no I/O, model call,
  subprocess, sleep or lock is introduced.

[Side-Effect Policy]
- Returns a copy of the service result; never writes knowledge or discards the
  original L3 observation from the caller's ledger.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping

MODES = frozenset({"reuse", "diagnose", "explore"})
OPTIONAL_KEYS = (
    "ledger_entries",
    "bottleneck_summary",
    "native_subcontext",
    "goal",
)
PACK_SCHEMA = "mindie-rsi-subagent-pack/1"
CONTEXT_SCHEMA = "mindie-exploration-context/1"
SCOPE_NOTE = (
    "Negative result under recorded conditions only; not a universal ban "
    "when conditions or bottleneck change."
)
TIER_BY_CATEGORY = {
    "axiom": "L1",
    "measurement": "L1",
    "sop": "L2",
    "workflow": "L3",
    "mutator": "L4",
}


def schema_properties() -> dict:
    """The exact optional MCP properties shared by query and explain."""
    return {
        "mode": {"type": "string", "enum": ["reuse", "diagnose", "explore"]},
        "ledger_entries": {"type": "array", "items": {"type": "object"}},
        "bottleneck_summary": {"type": "object"},
        "native_subcontext": {"type": "boolean"},
        "goal": {"type": "string"},
    }


def _tier(unit) -> str | None:
    tier = unit.get("tier")
    if isinstance(tier, str) and tier:
        return tier
    return TIER_BY_CATEGORY.get(unit.get("category"))


def _ident(unit, index: int) -> str:
    value = unit.get("id")
    if isinstance(value, str) and value:
        return value
    return f"{_tier(unit) or 'unit'}-{index}"


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


def _walk_units(result: Mapping) -> list:
    units, seen = [], set()

    def add(value):
        if isinstance(value, Mapping) and isinstance(value.get("id"), str):
            key = (value.get("id"), value.get("revision"))
            if key not in seen:
                seen.add(key)
                units.append(dict(value))

    for key in ("results", "cards", "foundation_cards", "units", "requires"):
        value = result.get(key)
        if isinstance(value, list):
            for item in value:
                add(item)
    if isinstance(result.get("id"), str):
        add(result)
    return units


def _promote(unit) -> list:
    promoted = []
    for key in ("inherited_rules", "foundation", "requires_units", "requires"):
        value = unit.get(key)
        if isinstance(value, list):
            promoted.extend(
                dict(item) for item in value
                if isinstance(item, Mapping) and _tier(item) != "L3"
            )
    return promoted


def _compact(unit) -> dict:
    return {
        "id": unit.get("id"),
        "tier": _tier(unit),
        "revision": unit.get("revision"),
        "category": unit.get("category"),
        "title": unit.get("title"),
        "conditions": unit.get("conditions"),
        "ref": unit.get("ref"),
        "body": unit.get("body") or unit.get("content") or unit.get("excerpt") or "",
    }


def _fallback_context(units, body) -> dict:
    mounted, stripped = [], []
    for index, unit in enumerate(units):
        if _tier(unit) == "L3":
            stripped.append(_ident(unit, index))
        elif _tier(unit) in {"L1", "L2", "L4"}:
            mounted.append(_compact(unit))
    mounted.sort(key=lambda item: (item["id"], item.get("revision") or 0))
    return {
        "schema": CONTEXT_SCHEMA,
        "mode": "explore",
        "goal": body.get("goal", ""),
        "native_subcontext": body.get("native_subcontext", False),
        "isolation_mode": (
            "native_subcontext"
            if body.get("native_subcontext")
            else "in_session_strategy_shift"
        ),
        "mounted_units": mounted,
        "units": mounted,
        "stripped_l3_ids": sorted(set(stripped)),
        "conditional_avoid": _conditional_avoid(body.get("ledger_entries") or []),
        "bottleneck_summary": body.get("bottleneck_summary") or {},
        "conditions": body.get("conditions") or {},
    }


def _conditional_avoid(ledger) -> list:
    avoid = []
    for entry in ledger:
        if entry.get("verdict") != "refuted":
            continue
        axes = entry.get("axes")
        if isinstance(axes, Mapping) and axes.get("evidence_validity") != "valid":
            continue
        conditions = entry.get("conditions")
        if conditions is None and isinstance(entry.get("applicability"), Mapping):
            conditions = entry["applicability"].get("conditions")
        avoid.append({
            "unit_id": entry.get("unit_id") or entry.get("candidate_id") or entry.get("run_id"),
            "hypothesis": entry.get("hypothesis") or entry.get("mutation_summary") or "",
            "failure_reason": entry.get("failure_reason") or entry.get("reason") or "",
            "observed_bottleneck": (
                entry.get("observed_bottleneck")
                or entry.get("bottleneck_summary")
                or ""
            ),
            "conditions": conditions if isinstance(conditions, Mapping) else {},
            "scope_note": SCOPE_NOTE,
        })
    return avoid


def _load_builder():
    try:
        from mindie_knowledge.graded import explore
    except (ImportError, AttributeError):
        return None
    builder = getattr(explore, "build_exploration_context", None)
    return builder if callable(builder) else None


def _build(builder, units, body) -> dict:
    ledger = body.get("ledger_entries") or []
    bottleneck = body.get("bottleneck_summary") or {}
    native = body.get("native_subcontext", False)
    goal = body.get("goal", "")
    conditions = body.get("conditions") or {}
    try:
        parameters = inspect.signature(builder).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "retrieved_units" in parameters or "ledger_entries" in parameters:
        return builder(
            goal=goal or "Exploration",
            retrieved_units=units,
            bottleneck_summary=bottleneck,
            ledger_entries=ledger,
            conditions=conditions,
            native_subcontext=native,
        )
    options = {
        "bottleneck_summary": bottleneck,
        "native_subcontext": native,
        "goal": goal,
    }
    if "failed_attempts" in parameters or "ledger_entries" in parameters:
        options["failed_attempts" if "failed_attempts" in parameters else "ledger_entries"] = ledger
    return builder(units, **options)


def _normalize(context, units, body, stripped) -> dict:
    value = dict(context)
    mounted = value.get("mounted_units")
    if not isinstance(mounted, list):
        mounted = value.get("units") if isinstance(value.get("units"), list) else []
    mounted = [dict(item) for item in mounted if isinstance(item, Mapping)]
    value["mounted_units"] = mounted
    value["units"] = mounted
    tiers = sorted({_tier(item) for item in mounted if _tier(item)})
    value.setdefault("mounted_tiers", tiers or ["L1", "L2", "L4"])
    value["stripped_tiers"] = ["L3"]
    ids = {item for item in value.get("stripped_l3_ids") or [] if isinstance(item, str)}
    ids.update(stripped)
    value["stripped_l3_ids"] = sorted(ids)
    value["conditional_avoid"] = _normalize_avoid(value.get("conditional_avoid"))
    value.setdefault("l1_foundation", [
        item for item in mounted if _tier(item) == "L1"
    ])
    value.setdefault("l2_bottleneck", dict(body.get("bottleneck_summary") or {}))
    value.setdefault("l4_mutators", [
        item for item in mounted if _tier(item) == "L4"
    ])
    native = body.get("native_subcontext", False)
    value["native_subcontext"] = native
    value["isolation_mode"] = (
        "native_subcontext" if native
        else value.get("isolation_mode", "in_session_strategy_shift")
    )
    value["requires_fresh_subcontext"] = not native
    value["goal"] = value.get("goal", body.get("goal", ""))
    pack = value.get("subagent_fact_pack")
    if not isinstance(pack, Mapping) or pack.get("schema_version") != PACK_SCHEMA:
        value["subagent_fact_pack"] = _fact_pack(value, body)
    return value


def _normalize_avoid(entries) -> list:
    if not isinstance(entries, list):
        return []
    normalized = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        conditions = entry.get("conditions")
        normalized.append({
            "unit_id": entry.get("unit_id") or entry.get("avoid") or entry.get("candidate_id"),
            "hypothesis": entry.get("hypothesis") or entry.get("avoid") or "",
            "failure_reason": entry.get("failure_reason") or entry.get("reason") or "",
            "observed_bottleneck": entry.get("observed_bottleneck") or entry.get("bottleneck") or "",
            "conditions": dict(conditions) if isinstance(conditions, Mapping) else {},
            "scope_note": entry.get("scope_note") or SCOPE_NOTE,
        })
    return normalized


def _fact_pack(context, body) -> dict:
    mounted = context.get("mounted_units") or []
    l1 = [item for item in mounted if _tier(item) == "L1"]
    l4 = [item for item in mounted if _tier(item) == "L4"]
    avoid = context.get("conditional_avoid") or []
    goal = str(context.get("goal") or body.get("goal") or "")
    bottleneck = context.get("l2_bottleneck") or body.get("bottleneck_summary") or {}
    native = bool(body.get("native_subcontext", False))
    return {
        "schema_version": PACK_SCHEMA,
        "goal": goal,
        "isolation_required": True,
        "isolation_mode": "native_subcontext" if native else "in_session_warning",
        "mounted_tiers": ["L1", "L2", "L4"],
        "stripped_tiers": ["L3"],
        "stripped_l3_ids": list(context.get("stripped_l3_ids") or []),
        "l1_foundation_ids": [item.get("id") for item in l1 if item.get("id")],
        "l2_bottleneck": dict(bottleneck) if isinstance(bottleneck, Mapping) else {},
        "l4_mutator_ids": [item.get("id") for item in l4 if item.get("id")],
        "conditional_avoid": avoid,
        "prompt_brief": _prompt_brief(goal, l1, bottleneck, l4, avoid),
    }


def _line(unit) -> str:
    title = unit.get("title") or unit.get("body") or unit.get("content") or ""
    text = str(title).replace("\n", " ").strip()
    return f"- {unit.get('id')}: {text[:180]}"


def _prompt_brief(goal, l1, bottleneck, l4, avoid) -> str:
    lines = [
        "# RSI exploration brief",
        f"Goal: {goal or '(not stated)'}",
        "## L1 hard constraints",
        *([_line(item) for item in l1] or ["- (none mounted; verify prerequisites before mutation)"]),
        "## L2 bottleneck evidence",
        f"- {str(dict(bottleneck) if isinstance(bottleneck, Mapping) else bottleneck)[:300]}",
        "## L4 allowed mutators",
        *([_line(item) for item in l4] or ["- (none mounted; propose a fresh orthogonal mutation)"]),
        "## Conditional avoid",
        *([
            f"- {item.get('hypothesis') or item.get('unit_id') or 'prior attempt'}"
            f" under {item.get('conditions')}: {item.get('failure_reason') or 'refuted'}"
            for item in avoid
        ] or ["- (none)"]),
        "## Isolation",
        "- Do not replay the prior L3 workflow trajectory; it was stripped.",
        "- Derive a new strategy from the L1/L2 facts and L4 mutators above.",
    ]
    brief = "\n".join(lines)
    return brief[:1180]


def attach(result, arguments):
    """Return an explore result with a guaranteed context and fact pack."""
    if not isinstance(result, Mapping):
        return result
    body = dict(arguments or {})
    if body.get("mode", "reuse") != "explore":
        return result
    units, stripped = _walk_units(result), set()
    promoted = []
    for index, unit in enumerate(units):
        if _tier(unit) == "L3":
            stripped.add(_ident(unit, index))
            promoted.extend(_promote(unit))
    units.extend(promoted)
    context = result.get("exploration_context")
    builder = _load_builder()
    if not isinstance(context, Mapping) and builder is not None:
        try:
            context = _build(builder, units, body)
        except (TypeError, ValueError, AttributeError):
            context = None
    if not isinstance(context, Mapping):
        context = _fallback_context(units, body)
    context = _normalize(context, units, body, stripped)
    shaped = dict(result)
    shaped["mode"] = "explore"
    shaped["exploration_context"] = context
    shaped["stripped_l3_ids"] = context["stripped_l3_ids"]
    for key in ("results", "cards", "foundation_cards", "units", "requires"):
        value = shaped.get(key)
        if not isinstance(value, list):
            continue
        shaped[key] = [
            item for item in value
            if not (isinstance(item, Mapping) and _tier(item) == "L3")
        ]
    foundation = shaped.get("foundation_cards")
    if promoted:
        foundation = list(foundation or [])
        known = {item.get("id") for item in foundation if isinstance(item, Mapping)}
        foundation.extend(item for item in promoted if item.get("id") not in known)
        shaped["foundation_cards"] = foundation
    return shaped


__all__ = ["MODES", "OPTIONAL_KEYS", "attach", "forwarded", "parse_args",
           "schema_properties"]
