#!/usr/bin/env python3
"""Objective-aware paired comparison for RSI benchmark receipts."""
from __future__ import annotations

import math
import random
from typing import Any, Mapping, Sequence

try:
    from rsi_workload import objective_spec, percentile, summarize_rows
except ImportError:  # pragma: no cover
    from .rsi_workload import objective_spec, percentile, summarize_rows

SCHEMA = "mindie-agent/rsi-comparison/v1"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _metric(row: Mapping[str, Any], metric: str) -> float | None:
    aliases = {
        "throughput": ("throughput", "output_tok_s", "tokens_per_s"),
        "ttft_p50": ("ttft_p50", "ttft_s"),
        "ttft_p95": ("ttft_p95", "ttft_s"),
        "latency_p50": ("latency_p50", "latency_s"),
        "latency_p95": ("latency_p95", "latency_s"),
        "peak_memory": ("peak_memory", "peak_memory_bytes", "memory_bytes"),
        "accepted_tokens_per_verify": ("accepted_tokens_per_verify", "accept_len", "accepted_tokens"),
    }.get(metric, (metric,))
    for name in aliases:
        value = row.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0:
            return float(value)
    return None


def _paired_rows(receipt: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    pairs = []
    for row in _rows(receipt.get("paired_runs")):
        baseline = row.get("baseline") if isinstance(row.get("baseline"), Mapping) else {}
        candidate = row.get("candidate") if isinstance(row.get("candidate"), Mapping) else {}
        if not baseline and not candidate:
            baseline = {key.removeprefix("baseline_"): value for key, value in row.items()
                        if key.startswith("baseline_")}
            candidate = {key.removeprefix("candidate_"): value for key, value in row.items()
                         if key.startswith("candidate_")}
        pairs.append((baseline, candidate))
    if pairs:
        return pairs
    baseline = _rows(receipt.get("baseline_samples"))
    candidate = _rows(receipt.get("candidate_samples"))
    return list(zip(baseline, candidate))


def _identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool | None:
    left_hash = left.get("output_ids_sha256") or left.get("output_hash")
    right_hash = right.get("output_ids_sha256") or right.get("output_hash")
    if left_hash is not None and right_hash is not None:
        return left_hash == right_hash
    left_ids = left.get("output_ids")
    right_ids = right.get("output_ids")
    if left_ids is not None and right_ids is not None:
        return left_ids == right_ids
    return None


def _bootstrap(values: Sequence[float], iterations: int = 2000) -> tuple[float, float, float] | None:
    if not values:
        return None
    point = math.exp(sum(math.log(value) for value in values) / len(values))
    if len(values) == 1:
        return point, point, point
    rng = random.Random(0)
    samples = []
    for _ in range(max(100, iterations)):
        sample = [values[rng.randrange(len(values))] for _ in values]
        samples.append(math.exp(sum(math.log(value) for value in sample) / len(sample)))
    samples.sort()
    low = samples[max(0, int(0.025 * len(samples)))]
    high = samples[min(len(samples) - 1, int(0.975 * len(samples)))]
    return point, low, high


def compare(receipt: Mapping[str, Any], objective: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    spec = objective_spec(objective, payload)
    metric = spec["metric"]
    maximize = spec["direction"] == "maximize"
    pairs = _paired_rows(receipt)
    ratios = []
    aggregate_values = None
    if metric in {"ttft_p50", "ttft_p95", "latency_p50", "latency_p95"}:
        baseline_values = [_metric(left, "ttft_s" if metric.startswith("ttft") else "latency_s")
                           for left, _ in pairs]
        candidate_values = [_metric(right, "ttft_s" if metric.startswith("ttft") else "latency_s")
                           for _, right in pairs]
        baseline_values = [value for value in baseline_values if value is not None]
        candidate_values = [value for value in candidate_values if value is not None]
        fraction = 0.95 if metric.endswith("p95") else 0.50
        base_stat = percentile(baseline_values, fraction)
        candidate_stat = percentile(candidate_values, fraction)
        if base_stat and candidate_stat and candidate_stat > 0:
            aggregate_values = (base_stat / candidate_stat if not maximize else candidate_stat / base_stat,)
    for baseline, candidate in pairs:
        left = _metric(baseline, metric)
        right = _metric(candidate, metric)
        if left is None or right is None:
            continue
        ratio = right / left if maximize else left / right
        if ratio > 0 and math.isfinite(ratio):
            ratios.append(ratio)
    ci = _bootstrap(aggregate_values or ratios)
    identity_rows = [_identity(left, right) for left, right in pairs]
    identity_known = [value for value in identity_rows if value is not None]
    accuracy = receipt.get("task_accuracy") or receipt.get("accuracy")
    result = {
        "schema": SCHEMA,
        "objective": spec,
        "metric": metric,
        "direction": spec["direction"],
        "paired_runs": len(ratios),
        "ratios": ratios,
        "geometric_ratio": ci[0] if ci else None,
        "ci_lower_ratio": ci[1] if ci else None,
        "ci_upper_ratio": ci[2] if ci else None,
        "baseline_summary": summarize_rows(_rows(receipt.get("baseline_samples"))),
        "candidate_summary": summarize_rows(_rows(receipt.get("candidate_samples"))),
        "output_identity": {
            "known_pairs": len(identity_known),
            "same_pairs": sum(value is True for value in identity_known),
            "mismatch_pairs": sum(value is False for value in identity_known),
            "all_known_match": bool(identity_known) and all(value is True for value in identity_known),
        },
        "task_accuracy": accuracy,
    }
    result["passed"] = bool(ci and ci[1] > float((payload or {}).get("min_speedup", 1.0)))
    return result
