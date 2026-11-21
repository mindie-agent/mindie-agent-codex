#!/usr/bin/env python3
"""Stock-target token-forcing scorer.

The scorer is intentionally model-agnostic.  Runtime adapters provide the
candidate token ids and the stock target token ids for the same prefixes.  The
module performs the strict, deterministic comparison and emits the evidence
contract consumed by the RSI gates.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "mindie-agent/rsi-token-forcing/v1"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _token(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def rows_from_sequences(candidate: Sequence[Any], stock: Sequence[Any], *,
                        prompt_id: str | None = None) -> list[dict[str, Any]]:
    """Build teacher-forcing rows from aligned candidate and stock token ids."""
    rows = []
    for position, (candidate_token, stock_token) in enumerate(zip(candidate, stock)):
        rows.append({"position": position, "prompt_id": prompt_id,
                     "candidate_token_id": candidate_token,
                     "stock_token_id": stock_token, "teacher_forced": True})
    return rows


def score(rows: Sequence[Mapping[str, Any]], *, margin_delta: float | None = None) -> dict[str, Any]:
    """Compare candidate and stock decisions for every teacher-forced position."""
    output: list[dict[str, Any]] = []
    mismatches = 0
    large_margin_mismatches = 0
    incomplete = 0
    for index, source in enumerate(rows):
        row = dict(source)
        position = _token(row, "position", "position_id", "token_index")
        candidate = _token(row, "candidate_token_id", "candidate_id", "candidate_token")
        stock = _token(row, "stock_token_id", "target_token_id", "stock_id", "stock_token")
        forced = row.get("teacher_forced") is True
        match = candidate is not None and stock is not None and candidate == stock
        if not forced or position is None or candidate is None or stock is None:
            incomplete += 1
        if not match:
            mismatches += 1
        margin = row.get("margin")
        try:
            margin_value = float(margin) if margin is not None else None
        except (TypeError, ValueError):
            margin_value = None
        large = margin_delta is not None and margin_value is not None and margin_value >= margin_delta
        if large and not match:
            large_margin_mismatches += 1
        output.append({
            "position": position if position is not None else index,
            "candidate_token_id": candidate,
            "stock_token_id": stock,
            "teacher_forced": forced,
            "match": match,
            "mismatch": not match,
            "margin": margin_value,
            "large_margin_mismatch": bool(large and not match),
            "prompt_id": row.get("prompt_id"),
        })
    passed = bool(output) and incomplete == 0 and mismatches == 0
    return {
        "schema": SCHEMA,
        "passed": passed,
        "token_forcing_rows": output,
        "positions": len(output),
        "mismatches": mismatches,
        "large_margin_mismatches": large_margin_mismatches,
        "incomplete_rows": incomplete,
        "margin_delta": margin_delta,
        "reason": "ok" if passed else "token_forcing_failed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON file containing token_forcing_rows or rows")
    parser.add_argument("--margin-delta", type=float, default=None)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    rows = payload.get("token_forcing_rows", payload.get("rows", [])) if isinstance(payload, Mapping) else []
    if isinstance(payload, Mapping) and not rows:
        candidate = payload.get("candidate_token_ids")
        stock = payload.get("stock_token_ids")
        if isinstance(candidate, list) and isinstance(stock, list):
            rows = rows_from_sequences(candidate, stock, prompt_id=payload.get("prompt_id"))
    result = score(_rows(rows), margin_delta=args.margin_delta)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
