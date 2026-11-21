#!/usr/bin/env python3
"""Bidirectional step-two search for speculative length."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from rsi_workload import DEFAULT_SPECULATIVE_LENGTH


def _number(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def assess_bound(observations: Sequence[Mapping[str, Any]],
                 parameter: str = "num_speculative_tokens",
                 metric: str = "throughput_tokens_s", *,
                 plateau_tolerance: float = 0.01,
                 regression_tolerance: float = 0.02,
                 utilization_threshold: float = 0.90,
                 origin: int | None = None,
                 step: int = 2) -> dict[str, Any]:
    """Stop each K direction at its first measured loss.

    A higher-K regression with high compute utilization stops the upward
    branch. A lower-K regression stops the downward branch. K=0 is proposed
    only after positive K values improve monotonically while moving down.
    """
    rows = []
    for row in observations:
        if not isinstance(row, Mapping):
            continue
        x = _number(row.get(parameter))
        y = _number(row.get(metric))
        if x is not None and y is not None and x >= 0:
            rows.append((int(x), y, row))
    rows.sort(key=lambda item: item[0])
    if not rows:
        return {"status": "insufficient_evidence", "bound": False, "stop": False,
                "samples": 0, "origin": DEFAULT_SPECULATIVE_LENGTH, "step": step,
                "search_direction": "bidirectional", "reason": "default_origin",
                "next_values": [max(0, DEFAULT_SPECULATIVE_LENGTH - step),
                                DEFAULT_SPECULATIVE_LENGTH + step]}
    by_k = {x: (y, row) for x, y, row in rows}
    positive = [x for x in by_k if x > 0]
    if origin is None:
        origin = positive[-1] if positive else DEFAULT_SPECULATIVE_LENGTH
    origin = max(0, int(origin))
    origin_value = by_k.get(origin, (None, {}))[0]
    if len(positive) < 2 or origin_value is None:
        next_values = sorted({value for value in (origin - step, origin + step)
                              if value >= 0 and value not in by_k})
        return {"status": "insufficient_evidence", "bound": False, "stop": False,
                "samples": len(rows), "origin": origin, "step": step,
                "search_direction": "bidirectional", "next_values": next_values,
                "reason": "probe_adjacent_values"}

    utilization = []
    for _, _, row in rows[-3:]:
        for key in ("compute_utilization", "ai_core_utilization", "aic_utilization",
                    "pipe_utilization", "utilization"):
            value = _number(row.get(key))
            if value is not None:
                utilization.append(value / 100.0 if value > 1.0 else value)
                break
    high_compute = bool(utilization) and sum(utilization) / len(utilization) >= utilization_threshold
    lower = sorted((x, by_k[x][0]) for x in by_k if 0 < x < origin)
    upper = sorted((x, by_k[x][0]) for x in by_k if x > origin)
    lower_improvements = [(right - left) / max(abs(left), 1e-12)
                          for (_, left), (_, right) in zip(lower, lower[1:])]
    upper_improvements = [(right - left) / max(abs(left), 1e-12)
                          for (_, left), (_, right) in zip(upper, upper[1:])]
    lower_loss = bool(lower and lower[-1][1] < origin_value * (1 - regression_tolerance))
    upper_loss = bool(upper and upper[0][1] < origin_value * (1 - regression_tolerance))
    up_stop = upper_loss and high_compute
    down_stop = lower_loss
    lower_consistently_better = bool(
        lower and all(value < 0 for value in lower_improvements)
        and lower[0][1] > lower[-1][1] > origin_value
    )
    test_zero = lower_consistently_better and lower[0][0] <= step and 0 not in by_k
    next_values = []
    if not up_stop:
        next_values.append((upper[-1][0] if upper else origin) + step)
    if not down_stop:
        candidate = (lower[0][0] if lower else origin) - step
        if candidate >= 0:
            next_values.append(candidate)
    if test_zero:
        next_values.append(0)
    improvements = [
        (current[1] - previous[1]) / max(abs(previous[1]), 1e-12)
        for previous, current in zip(rows, rows[1:])
    ]
    tail = improvements[-2:]
    plateau = len(tail) == 2 and all(abs(value) <= plateau_tolerance for value in tail)
    regression = len(tail) == 2 and all(value <= -regression_tolerance for value in tail)
    bound = plateau or regression or (up_stop and down_stop)
    reason = ("compute_bound_upward" if up_stop else
              "downward_regression" if down_stop else
              "throughput_plateau" if plateau else
              "throughput_regression" if regression else "directional_search")
    return {"status": "bound" if bound else "expand", "bound": bound, "stop": bound,
            "samples": len(rows), "parameter": parameter, "metric": metric,
            "improvements": improvements, "lower_improvements": lower_improvements,
            "upper_improvements": upper_improvements, "utilization_tail": utilization,
            "origin": origin, "step": step, "high_compute": high_compute,
            "direction_up": "stop" if up_stop else "continue",
            "direction_down": "stop" if down_stop else "continue",
            "test_zero": test_zero, "next_values": sorted(set(next_values)),
            "reason": reason,
            "next_action": "stop_or_select_best" if bound else "probe_adjacent_by_two"}
