#!/usr/bin/env python3
"""Normalize common Ascend msprof CSV/JSON exports for RSI cost modeling."""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "mindie-agent/rsi-ascend-profile/v1"
_STAGE_PATTERNS = {
    "draft": (r"draft", r"drafter", r"speculate"),
    "verify": (r"verify", r"accept", r"target.*forward", r"decode"),
    "scheduler": (r"schedul", r"queue", r"batch"),
    "memory": (r"memcpy", r"copy", r"hbm", r"cache", r"read"),
}


def _number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _duration_s(row: Mapping[str, Any]) -> float:
    for name, scale in (("duration_s", 1.0), ("duration_ms", 1e-3),
                        ("duration_us", 1e-6), ("dur", 1e-6),
                        ("Duration(us)", 1e-6), ("Duration(us)", 1e-6)):
        value = _number(row.get(name))
        if value is not None:
            return max(0.0, value * scale)
    return 0.0


def _name(row: Mapping[str, Any]) -> str:
    for key in ("name", "kernel_name", "op_name", "Name", "Kernel Name", "Op Name"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def classify_stage(name: str) -> str:
    lowered = name.lower()
    for stage, patterns in _STAGE_PATTERNS.items():
        if any(re.search(pattern, lowered) for pattern in patterns):
            return stage
    return "unknown"


def _load_rows(path: Path) -> list[Mapping[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    for key in ("rows", "records", "events", "kernel_details"):
        if isinstance(value, Mapping) and isinstance(value.get(key), list):
            return [row for row in value[key] if isinstance(row, Mapping)]
    return []


def parse(paths: Iterable[str | Path]) -> dict[str, Any]:
    stages: dict[str, float] = {key: 0.0 for key in _STAGE_PATTERNS}
    kernels: list[dict[str, Any]] = []
    source_files: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        source_files.append(str(path))
        for row in _load_rows(path):
            name = _name(row)
            stage = str(row.get("stage") or classify_stage(name))
            duration_s = _duration_s(row)
            if stage in stages:
                stages[stage] += duration_s
            kernels.append({
                "name": name,
                "stage": stage,
                "duration_s": duration_s,
                "flops": _number(row.get("flops") or row.get("FLOPs")),
                "bytes": _number(row.get("bytes") or row.get("Bytes") or row.get("memory_bytes")),
                "utilization": _number(row.get("utilization") or row.get("PipeUtilization")),
            })
    total = sum(stages.values())
    return {
        "schema": SCHEMA,
        "source_files": source_files,
        "kernel_count": len(kernels),
        "stages": stages,
        "stage_fractions": {key: value / total for key, value in stages.items()} if total else {},
        "kernel_rows": kernels,
        "profile_quality": {
            "has_stage_timings": total > 0,
            "has_kernel_rows": bool(kernels),
            "has_roofline_counters": any(row["flops"] is not None or row["bytes"] is not None for row in kernels),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(parse(args.paths), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
