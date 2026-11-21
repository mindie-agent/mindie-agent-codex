#!/usr/bin/env python3
"""Validate the evidence contract exchanged by RSI runtime adapters.

The runtime may use MindIE's native profiler, benchmark CLI, or a customer
adapter, but the controller consumes one normalized contract. Missing rows,
unpaired requests, and incomplete token forcing receipts fail closed.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA = "mindie-agent/rsi-evidence/v1"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _number(row: Mapping[str, Any], names: Sequence[str]) -> float | None:
    stages = row.get("stage_times") or row.get("stages")
    if isinstance(stages, Mapping):
        for name in names:
            if name in stages:
                value = stages[name]
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
    for name in names:
        value = row.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _profile_rows(receipt: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("profile_observations", "observations", "samples", "rows"):
        rows = _rows(receipt.get(key))
        if rows:
            return rows
    return []


def validate_profile(receipt: Mapping[str, Any], *, min_samples: int = 5) -> dict[str, Any]:
    rows = _profile_rows(receipt)
    required = {
        "elapsed": ("elapsed_s", "elapsed_ms", "latency_s", "latency_ms"),
        "completion_tokens": ("completion_tokens", "output_tokens", "generated_tokens"),
        "draft": ("draft_s", "draft_ms", "draft_time_s"),
        "verify": ("verify_s", "verify_ms", "verify_time_s"),
        "scheduler": ("scheduler_s", "scheduler_ms", "scheduler_time_s"),
        "memory": ("memory_s", "memory_ms", "memory_time_s"),
    }
    missing_fields = [name for name, aliases in required.items()
                      if not any(_number(row, aliases) is not None for row in rows)]
    passed = len(rows) >= max(1, int(min_samples)) and not missing_fields
    return {
        "schema": SCHEMA,
        "kind": "profile",
        "passed": passed,
        "samples": len(rows),
        "min_samples": max(1, int(min_samples)),
        "missing_fields": missing_fields,
        "reason": "ok" if passed else "profile_evidence_incomplete",
    }


def _paired_rows(receipt: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = _rows(receipt.get("paired_runs"))
    if rows:
        return rows
    speedups = receipt.get("paired_speedups")
    if isinstance(speedups, list):
        return [{"speedup": value} for value in speedups
                if isinstance(value, (int, float)) and not isinstance(value, bool)]
    baseline = _rows(receipt.get("baseline_samples"))
    candidate = _rows(receipt.get("candidate_samples"))
    if baseline and candidate:
        return [
            {"baseline_time_s": left.get("elapsed_s", left.get("latency_s")),
             "candidate_time_s": right.get("elapsed_s", right.get("latency_s")),
             "baseline_tokens": left.get("completion_tokens"),
             "candidate_tokens": right.get("completion_tokens"),
             "request_index": index}
            for index, (left, right) in enumerate(zip(baseline, candidate))
        ]
    return []


def validate_paired(receipt: Mapping[str, Any], *, min_runs: int = 5) -> dict[str, Any]:
    rows = _paired_rows(receipt)
    valid = 0
    for row in rows:
        speedup = _number(row, ("speedup", "paired_speedup"))
        baseline = _number(row, ("baseline_time_s", "baseline_latency_s", "baseline_elapsed_s"))
        candidate = _number(row, ("candidate_time_s", "candidate_latency_s", "candidate_elapsed_s"))
        baseline_rate = _number(row, ("baseline_output_tok_s", "baseline_throughput", "baseline_tokens_per_s"))
        candidate_rate = _number(row, ("candidate_output_tok_s", "candidate_throughput", "candidate_tokens_per_s"))
        if speedup is not None or (baseline is not None and candidate is not None and candidate > 0) \
                or (baseline_rate is not None and candidate_rate is not None and candidate_rate > 0):
            valid += 1
    passed = valid >= max(1, int(min_runs))
    return {
        "schema": SCHEMA,
        "kind": "paired_performance",
        "passed": passed,
        "runs": valid,
        "min_runs": max(1, int(min_runs)),
        "reason": "ok" if passed else "paired_runs_incomplete",
    }


def validate_teacher_forcing(receipt: Mapping[str, Any], *, min_rows: int = 1) -> dict[str, Any]:
    rows = _rows(receipt.get("token_forcing_rows"))
    if not rows:
        for prompt in _rows(receipt.get("prompts")):
            rows.extend(_rows(prompt.get("positions")))
    mismatch = 0
    position_count = 0
    forced = True
    fields_complete = bool(rows)
    for row in rows:
        forced = forced and row.get("teacher_forced") is True
        has_position = any(key in row for key in ("position", "position_id", "token_index"))
        has_match = any(key in row for key in ("token_match", "match", "mismatch"))
        fields_complete = fields_complete and has_position and has_match
        position_count += 1 if has_position else 0
        mismatch += 1 if row.get("mismatch") is True or row.get("match") is False \
            or row.get("token_match") is False else 0
    passed = (len(rows) >= max(1, int(min_rows)) and position_count == len(rows)
              and fields_complete and forced and mismatch == 0)
    return {
        "schema": SCHEMA,
        "kind": "teacher_forcing",
        "passed": passed,
        "positions": len(rows),
        "mismatches": mismatch,
        "teacher_forced": forced,
        "reason": "ok" if passed else "teacher_forcing_evidence_incomplete",
    }


def validate_receipts(receipts: Mapping[str, Any], *, min_profile_samples: int = 5,
                      min_paired_runs: int = 5, min_teacher_rows: int = 1) -> dict[str, Any]:
    profile = validate_profile(receipts.get("profile", {}), min_samples=min_profile_samples)
    paired = validate_paired(receipts.get("performance", {}), min_runs=min_paired_runs)
    teacher = validate_teacher_forcing(receipts.get("accuracy", {}), min_rows=min_teacher_rows)
    return {"schema": SCHEMA, "passed": profile["passed"] and paired["passed"] and teacher["passed"],
            "profile": profile, "paired_performance": paired, "teacher_forcing": teacher}
