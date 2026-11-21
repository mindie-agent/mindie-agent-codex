#!/usr/bin/env python3
"""Build privacy-safe, reproducible RSI workload manifests."""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from typing import Any, Mapping, Sequence

SCHEMA = "mindie-agent/rsi-workload/v1"
DEFAULT_SPECULATIVE_LENGTH = 5
DEFAULT_WARMUP_RUNS = 1
DEFAULT_MEASURED_REPEATS = 3


def discover_startup_parameter(text: str, parameter: str = "num_speculative_tokens") -> int | None:
    """Extract a numeric speculative parameter from shell flags or JSON config."""
    if not isinstance(text, str):
        return None
    escaped = re.escape(parameter).replace("_", "[-_]")
    patterns = (
        rf"(?:--{escaped}|{escaped})\s*(?:=|:|\s)\s*['\"]?([0-9]+)",
        rf"['\"]{escaped}['\"]\s*:\s*['\"]?([0-9]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def objective_spec(objective: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the primary metric from the user's objective without guessing a workload."""
    value = payload or {}
    explicit = value.get("objective_metric")
    if isinstance(explicit, str) and explicit.strip():
        metric = explicit.strip().lower()
    else:
        text = str(objective or "").lower()
        metric = ("ttft_p95" if "p95" in text and "ttft" in text else "ttft_p50") if any(word in text for word in ("ttft", "首 token", "首token")) else (
            "latency_p95" if any(word in text for word in ("p95", "tail latency", "尾延迟")) else (
                "latency_p50" if any(word in text for word in ("latency", "延迟")) else (
                    "peak_memory" if any(word in text for word in ("memory", "显存", "内存")) else (
                        "accepted_tokens_per_verify" if "accept" in text or "接受率" in text else "throughput"))))
    direction = "minimize" if metric in {"ttft_p50", "ttft_p95", "latency_p50", "latency_p95", "peak_memory"} else "maximize"
    return {"metric": metric, "direction": direction,
            "constraints": dict(value.get("objective_constraints") or {})}


def build_manifest(payload: Mapping[str, Any], *, startup_text: str | None = None,
                   test_text: str | None = None) -> dict[str, Any]:
    """Normalize user supplied test semantics while keeping host paths out of the snapshot."""
    supplied = payload.get("workload_manifest")
    manifest = dict(supplied) if isinstance(supplied, Mapping) else {}
    objective = objective_spec(str(payload.get("objective") or ""), payload)
    command = payload.get("test_command")
    manifest.update({
        "schema": SCHEMA,
        "objective": objective,
        "dataset": manifest.get("dataset") or payload.get("dataset"),
        "dataset_revision": manifest.get("dataset_revision") or payload.get("dataset_revision"),
        "batch_size": manifest.get("batch_size", payload.get("batch_size")),
        "concurrency": manifest.get("concurrency", payload.get("concurrency", 1)),
        "max_new_tokens": manifest.get("max_new_tokens", payload.get("max_new_tokens")),
        "input_tokens": manifest.get("input_tokens", payload.get("input_tokens")),
        "temperature": manifest.get("temperature", payload.get("temperature", 0)),
        "top_p": manifest.get("top_p", payload.get("top_p", 1)),
        "seed": manifest.get("seed", payload.get("seed")),
        "warmup_runs": max(0, int(manifest.get("warmup_runs", payload.get("warmup_runs", DEFAULT_WARMUP_RUNS)) or 0)),
        "repeats": max(1, int(manifest.get("repeats", payload.get("repeats", DEFAULT_MEASURED_REPEATS)) or 1)),
        "prompt_ids": list(manifest.get("prompt_ids") or payload.get("prompt_ids") or []),
        "model_revision": manifest.get("model_revision") or payload.get("model_revision"),
        "tokenizer_revision": manifest.get("tokenizer_revision") or payload.get("tokenizer_revision"),
        "test_command_sha256": digest(command) if command else None,
        "startup_sha256": digest(startup_text) if startup_text else None,
        "test_definition_sha256": digest(test_text) if test_text else None,
        "startup_parameters": dict(manifest.get("startup_parameters") or {}),
    })
    value = discover_startup_parameter(startup_text or "")
    manifest["startup_parameters"].setdefault(
        "num_speculative_tokens", value if value is not None else DEFAULT_SPECULATIVE_LENGTH)
    manifest["startup_parameters"].setdefault(
        "num_speculative_tokens_source", "startup_script" if value is not None else "default")
    return manifest


def request_record(row: Mapping[str, Any], *, run_index: int, phase: str = "measured") -> dict[str, Any]:
    """Normalize one benchmark response row for paired objective comparison."""
    result = dict(row)
    result["run_index"] = int(run_index)
    result["phase"] = phase
    result["request_id"] = result.get("request_id") or result.get("id") or result.get("prompt_id") or f"row-{run_index}"
    result["id"] = result["request_id"]
    result["prompt_id"] = result.get("prompt_id") or result["request_id"]
    result["input_tokens"] = result.get("input_tokens", result.get("prompt_tokens"))
    result["output_tokens"] = result.get("output_tokens", result.get("completion_tokens"))
    result["completion_tokens"] = result.get("completion_tokens", result["output_tokens"])
    result["latency_s"] = result.get("latency_s", result.get("request_latency_s"))
    result["ttft_s"] = result.get("ttft_s", result.get("first_token_latency_s"))
    result["verify_steps"] = result.get("verify_steps", result.get("spec_verify_ct"))
    result["accepted_tokens"] = result.get("accepted_tokens", result.get("spec_accepted_tokens"))
    if result.get("decode_tokens_per_s") is None:
        elapsed = result.get("latency_s")
        tokens = result.get("output_tokens")
        if isinstance(elapsed, (int, float)) and elapsed > 0 and isinstance(tokens, (int, float)):
            result["decode_tokens_per_s"] = float(tokens) / float(elapsed)
    for source, target in (("input_ids", "input_ids_sha256"), ("output_ids", "output_ids_sha256")):
        if target not in result and result.get(source) is not None:
            result[target] = digest(result[source])
    if result.get("output_ids") is not None:
        result["output_id_count"] = len(result["output_ids"]) if isinstance(result["output_ids"], Sequence) else None
    # Task correctness is recorded separately from output identity.  A task
    # score can pass even when output ids differ, and identity can match while
    # a task-specific scorer is unavailable.
    result["task_accuracy"] = result.get("task_accuracy", result.get("accuracy_score"))
    return result


def percentile(values: Sequence[float], fraction: float) -> float | None:
    clean = sorted(float(value) for value in values if isinstance(value, (int, float)))
    if not clean:
        return None
    index = min(len(clean) - 1, max(0, int(round((len(clean) - 1) * fraction))))
    return clean[index]


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    latencies = [row.get("latency_s") for row in rows]
    ttft = [row.get("ttft_s") for row in rows]
    memory = [row.get("peak_memory_bytes") for row in rows]
    tokens = sum(float(row.get("completion_tokens", 0) or 0) for row in rows)
    elapsed = sum(float(row.get("latency_s", 0) or 0) for row in rows)
    accepted = sum(float(row.get("accepted_tokens", 0) or 0) for row in rows)
    verifies = sum(float(row.get("verify_steps", row.get("spec_verify_ct", 0)) or 0) for row in rows)
    return {
        "requests": len(rows),
        "output_tokens": tokens,
        "throughput": tokens / elapsed if elapsed > 0 else None,
        "latency_p50": percentile(latencies, 0.50),
        "latency_p95": percentile(latencies, 0.95),
        "ttft_p50": percentile(ttft, 0.50),
        "ttft_p95": percentile(ttft, 0.95),
        "peak_memory": max((float(value) for value in memory if isinstance(value, (int, float))), default=None),
        "accepted_tokens_per_verify": accepted / verifies if verifies > 0 else None,
        "mean_latency": statistics.mean(float(value) for value in latencies if isinstance(value, (int, float))) if any(isinstance(value, (int, float)) for value in latencies) else None,
        "task_accuracy": [row.get("task_accuracy") for row in rows if row.get("task_accuracy") is not None],
    }
