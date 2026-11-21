#!/usr/bin/env python3
"""Turn profiler observations into a conservative RSI ceiling report.

[Role] Implements the runtime-neutral cost-model phase used before proposing
       source/configuration candidates.
[Invariants] Missing stage timings reduce confidence and never become a
             measured paper claim; all output is JSON-serialisable.
[Timeouts & Locks] Pure computation; it starts no workload and owns no NPU.
[Side-Effect Policy] Reads mappings and returns a report; callers decide where
                     to persist it.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, asdict
from typing import Any, Mapping, Sequence

SCHEMA = "mindie-agent/rsi-cost-model/v1"
_EPS = 1e-12


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default


def _first(row: Mapping[str, Any], *names: str, default: float = 0.0) -> float:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return _number(row[name], default)
    return default


def _seconds(row: Mapping[str, Any], *names: str) -> float:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return max(0.0, _number(row[name]))
    for name in names:
        stem = name[:-2] if name.endswith("_s") else name
        for suffix, scale in (("_ms", 1e-3), ("_us", 1e-6)):
            alias = stem + suffix
            if alias in row and row[alias] not in (None, ""):
                return max(0.0, _number(row[alias]) * scale)
    return 0.0


@dataclass(frozen=True)
class Observation:
    elapsed_s: float
    completion_tokens: int
    accepted_tokens: int = 0
    proposed_tokens: int = 0
    rounds: int = 0
    draft_s: float = 0.0
    verify_s: float = 0.0
    scheduler_s: float = 0.0
    memory_s: float = 0.0
    baseline_elapsed_s: float = 0.0
    baseline_tokens: int = 0

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "Observation":
        completion = int(round(_first(row, "completion_tokens", "output_tokens",
                                       "generated_tokens", "tokens")))
        return cls(
            elapsed_s=_seconds(row, "elapsed_s", "wall_s", "latency_s",
                               "time_s", "request_time_s"),
            completion_tokens=max(0, completion),
            accepted_tokens=max(0, int(round(_first(
                row, "accepted_tokens", "spec_accepted_tokens")))),
            proposed_tokens=max(0, int(round(_first(
                row, "proposed_tokens", "draft_tokens",
                "num_speculative_tokens")))),
            rounds=max(0, int(round(_first(
                row, "rounds", "speculative_rounds", "verify_steps")))),
            draft_s=_seconds(row, "draft_time_s", "draft_latency_s", "draft_ms"),
            verify_s=_seconds(row, "verify_time_s", "verification_time_s",
                              "target_verify_time_s", "verify_ms"),
            scheduler_s=_seconds(row, "scheduler_time_s", "queue_time_s",
                                 "scheduler_ms"),
            memory_s=_seconds(row, "memory_time_s", "memory_stall_s", "hbm_stall_s"),
            baseline_elapsed_s=_seconds(row, "baseline_elapsed_s",
                                        "baseline_latency_s", "ar_elapsed_s",
                                        "baseline_ms"),
            baseline_tokens=max(0, int(round(_first(
                row, "baseline_tokens", "baseline_completion_tokens",
                default=completion)))),
        )


def _ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if abs(denominator) > _EPS else default


def diagnose(observations: Sequence[Mapping[str, Any] | Observation], *,
             peak_flops_per_s: float | None = None,
             peak_bytes_per_s: float | None = None,
             kernel_rows: Sequence[Mapping[str, Any]] | None = None,
             tree_replay: Mapping[str, Any] | None = None,
             overhead_s: float = 0.0) -> dict[str, Any]:
    """Return a bounded ceiling, measured gap, and ranked bottleneck hints.

    The ceiling is deliberately conservative when stage timings or baseline
    measurements are absent. This is a planning signal, not a promotion gate.
    """
    parsed = [item if isinstance(item, Observation) else Observation.from_mapping(item)
              for item in observations]
    evidence: list[str] = []
    if not parsed:
        return {"schema": SCHEMA, "samples": 0, "confidence": 0.0,
                "largest_gap": "unknown", "evidence": ["no_samples"]}
    elapsed = sum(item.elapsed_s for item in parsed)
    tokens = sum(item.completion_tokens for item in parsed)
    baseline_per_token = [
        item.baseline_elapsed_s / item.baseline_tokens
        for item in parsed
        if item.baseline_elapsed_s > 0 and item.baseline_tokens > 0
    ]
    observed_per_token = [
        item.elapsed_s / item.completion_tokens
        for item in parsed if item.elapsed_s > 0 and item.completion_tokens > 0
    ]
    if not baseline_per_token:
        baseline_per_token = observed_per_token[:]
        evidence.append("baseline_time_inferred")
    if not observed_per_token:
        evidence.append("completion_tokens_missing")
    baseline_time = statistics.median(baseline_per_token) if baseline_per_token else 0.0
    observed_time = _ratio(elapsed, tokens)
    observed_speedup = _ratio(baseline_time, observed_time, 1.0)
    accepted = sum(item.accepted_tokens for item in parsed)
    proposed = sum(item.proposed_tokens for item in parsed)
    rounds = sum(item.rounds for item in parsed)
    accepted_per_round = _ratio(accepted, rounds)
    effective_tokens = max(1.0, 1.0 + accepted_per_round)
    stage = {
        "draft": sum(item.draft_s for item in parsed),
        "verify": sum(item.verify_s for item in parsed),
        "scheduler": sum(item.scheduler_s for item in parsed),
        "memory": sum(item.memory_s for item in parsed),
    }
    measured_stage_total = sum(stage[key] for key in ("draft", "verify", "scheduler"))
    if measured_stage_total <= _EPS:
        measured_stage_total = elapsed
        evidence.append("stage_times_missing")
        ceiling = observed_speedup * 2.0
        evidence.append("ceiling_bounded_without_stage_profile")
    else:
        ideal_time = _ratio(measured_stage_total,
                            effective_tokens * len(parsed), measured_stage_total)
        ceiling = max(observed_speedup, _ratio(baseline_time, ideal_time,
                                               observed_speedup))
    ceiling = max(1.0, ceiling)
    gap = max(0.0, ceiling - observed_speedup)
    roofline = _roofline(kernel_rows or [], peak_flops_per_s, peak_bytes_per_s)
    replay = _tree_replay(tree_replay)
    if roofline["time_floor_s"] > 0 and baseline_time > 0:
        floor_time = roofline["time_floor_s"] + max(0.0, float(overhead_s))
        replay_time = replay.get("best_exact_time_s") or 0.0
        if replay_time > 0:
            floor_time += replay_time
        ceiling = max(1.0, _ratio(baseline_time, floor_time, ceiling))
        gap = max(0.0, ceiling - observed_speedup)
    total_stage = max(measured_stage_total + stage["memory"], _EPS)
    fractions = {key: _ratio(value, total_stage) for key, value in stage.items()}
    largest_gap = max((key for key in fractions if key != "memory"),
                      key=lambda key: fractions[key], default="unknown")
    if fractions["memory"] > fractions.get(largest_gap, 0.0):
        largest_gap = "memory"
    if peak_flops_per_s is not None:
        evidence.append("peak_flops_supplied")
    if peak_bytes_per_s is not None:
        evidence.append("peak_bytes_supplied")
    confidence = min(1.0, 0.15 + 0.12 * min(len(parsed), 5)
                     + (0.35 if baseline_per_token else 0.0)
                     + (0.25 if measured_stage_total != elapsed else 0.0)
                     + (0.15 if proposed and rounds else 0.0))
    return {
        "schema": SCHEMA,
        "samples": len(parsed),
        "elapsed_s": elapsed,
        "completion_tokens": tokens,
        "throughput_tokens_s": _ratio(tokens, elapsed),
        "observed_speedup": observed_speedup,
        "ceiling_speedup": ceiling,
        "headroom": gap,
        "accepted_rate": _ratio(accepted, proposed),
        "accepted_tokens_per_round": accepted_per_round,
        "stage_fractions": fractions,
        "largest_gap": largest_gap,
        "confidence": confidence,
        "evidence": evidence,
        "hardware": {"peak_flops_per_s": peak_flops_per_s,
                      "peak_bytes_per_s": peak_bytes_per_s},
        "roofline": roofline,
        "tree_replay": replay,
        "overhead_s": max(0.0, float(overhead_s)),
    }


def _roofline(rows: Sequence[Mapping[str, Any]], peak_flops: float | None,
              peak_bytes: float | None) -> dict[str, Any]:
    """Compute a conservative per-kernel time floor when counters exist."""
    floors = []
    for row in rows:
        flops = _number(row.get("flops"), 0.0)
        bytes_read = _number(row.get("bytes"), 0.0)
        candidates = []
        if peak_flops and flops > 0:
            candidates.append(flops / peak_flops)
        if peak_bytes and bytes_read > 0:
            candidates.append(bytes_read / peak_bytes)
        if candidates:
            floors.append(max(candidates))
    return {
        "kernel_count": len(rows),
        "measured_kernel_count": len(floors),
        "time_floor_s": sum(floors),
        "coverage": _ratio(len(floors), len(rows)) if rows else 0.0,
        "method": "max(flops/peak_flops, bytes/peak_bytes)",
    }


def _tree_replay(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fold optional replay results without inventing a result when absent."""
    if not isinstance(value, Mapping):
        return {"available": False, "candidate_count": 0}
    candidates = value.get("candidates")
    rows = [item for item in candidates if isinstance(item, Mapping)] if isinstance(candidates, list) else []
    best = value.get("best_exact_time_s")
    try:
        best_value = float(best) if best is not None else 0.0
    except (TypeError, ValueError):
        best_value = 0.0
    return {
        "available": bool(rows) or best_value > 0,
        "candidate_count": len(rows),
        "best_exact_time_s": max(0.0, best_value),
        "best_candidate_id": value.get("best_candidate_id"),
    }


def plan_from_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = payload.get("profile_observations")
    if not isinstance(rows, list):
        return None
    model = payload.get("cost_model")
    model = model if isinstance(model, Mapping) else {}
    profile = payload.get("profile") if isinstance(payload.get("profile"), Mapping) else {}
    kernel_rows = profile.get("kernel_rows") or payload.get("kernel_rows") or []
    return diagnose(rows, peak_flops_per_s=_number(model.get("peak_flops_per_s"), 0.0)
                    or None, peak_bytes_per_s=_number(model.get("peak_bytes_per_s"), 0.0)
                    or None, kernel_rows=kernel_rows,
                    tree_replay=payload.get("tree_replay"),
                    overhead_s=_number(model.get("overhead_s"), 0.0))
