#!/usr/bin/env python3
"""Dispatch the standard RSI entrypoint using a customer binding file.

[Role] Provides one stable entrypoint for every installed RSI runtime.
[Invariants] The selected runtime entry is a validated relative Python path;
             binding data is passed as JSON and never interpreted as shell.
[Timeouts & Locks] The selected runtime owns leases, jobs, and its timeout;
                    this wrapper does not retry a child process.
[Side-Effect Policy] Reads stdin and binding JSON, then delegates one fixed
                     child process; it writes no files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,191}$")


def _entry(value: Any) -> str:
    if not isinstance(value, str) or not _RELATIVE.fullmatch(value):
        raise ValueError("runtime_entry must be a relative path")
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError("runtime_entry cannot escape rsi_root")
    return value


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("bindings must be a JSON object")
    return value


def _select_binding(bindings: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    default = bindings.get("default")
    if isinstance(default, Mapping):
        selected.update(default)
    elif all(isinstance(k, str) for k in bindings):
        selected.update(bindings)

    candidate = payload.get("candidate")
    candidate_id = candidate.get("candidate_id") if isinstance(candidate, Mapping) else None
    candidates = bindings.get("candidates")
    if candidate_id and isinstance(candidates, Mapping):
        value = candidates.get(candidate_id)
        if isinstance(value, Mapping):
            selected.update(value)
    elif candidate_id and isinstance(candidates, list):
        for value in candidates:
            if isinstance(value, Mapping) and value.get("candidate_id") == candidate_id:
                selected.update(value)
                break
    return selected


def _binding_path(root: Path, explicit: str | None) -> Path:
    value = explicit or os.environ.get("RSI_BINDINGS")
    return Path(value) if value else root / "config" / "live-runtime-bindings.json"


def dispatch(operation: str, root: Path, bindings_path: str | None,
             payload: Mapping[str, Any]) -> int:
    path = _binding_path(root, bindings_path)
    if not path.exists():
        raise FileNotFoundError(f"binding file not found: {path}")
    binding = _select_binding(_load(path), payload)
    runtime_entry = _entry(binding.get("runtime_entry") or binding.get("entry"))
    target = root / runtime_entry
    if not target.is_file():
        raise FileNotFoundError(f"runtime entry not found: {target}")
    runtime_python = binding.get("runtime_python", sys.executable)
    if not isinstance(runtime_python, str) or not runtime_python.strip():
        raise ValueError("runtime_python must be a non-empty string")
    child_payload = dict(payload)
    child_payload["runtime_binding"] = binding
    child_payload["bindings_file"] = str(path)
    process = subprocess.run(
        [runtime_python, str(target), operation, "--root", str(root)],
        input=json.dumps(child_payload, ensure_ascii=False, sort_keys=True),
        text=True,
        cwd=str(root),
        check=False,
    )
    return process.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bindings", default=None)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a JSON object")
        return dispatch(args.operation, args.root, args.bindings, payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
