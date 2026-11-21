#!/usr/bin/env python3
"""Reference JSON runtime adapter for an installed RSI root.

The adapter is host-neutral. A customer supplies argv-style commands in
``config/live-runtime-bindings.json`` (or through the controller payload); the
adapter executes one RSI operation, parses the final JSON receipt, and stores
an immutable local receipt. It never invokes a shell and never turns missing
evidence into a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

COMMAND_KEYS = {
    "doctor": ("doctor_cmd",),
    "status": ("status_cmd",),
    "prepare": ("prepare_cmd",),
    "build": ("build_cmd",),
    "profile": ("profile_cmd", "profiler_cmd"),
    "accuracy": ("teacher_forcing_scorer_cmd", "token_forcing_cmd", "accuracy_cmd"),
    "performance": ("benchmark_cmd", "performance_cmd"),
    "cleanup": ("cleanup_cmd",),
}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def _binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("runtime_binding")
    return dict(value) if isinstance(value, Mapping) else {}


def _commands(binding: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source in (binding.get("commands"), binding, payload, payload.get("controls")):
        if not isinstance(source, Mapping):
            continue
        for key in COMMAND_KEYS.values():
            for name in key:
                if name in source and name not in result:
                    result[name] = source[name]
        # A plan-level test_command is an explicit benchmark argv. Reuse it
        # as the performance command when no deployment binding overrides it.
        if "test_command" in source and "benchmark_cmd" not in result:
            result["benchmark_cmd"] = source["test_command"]
    return result


def _argv(value: Any, field: str) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        result = shlex.split(value)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result = [str(item) for item in value]
    else:
        raise ValueError(f"{field} must be an argv array or command string")
    if not result or any(not item for item in result):
        raise ValueError(f"{field} must contain non-empty argv values")
    return result


def _environment(binding: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, str]:
    result = dict(os.environ)
    for source in (binding.get("env"), payload.get("env")):
        if not isinstance(source, Mapping):
            continue
        for key, value in source.items():
            if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
                raise ValueError(f"invalid environment name: {key!r}")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"environment value must be scalar: {key}")
            result[key] = str(value)
    return result


def _receipt(root: Path, operation: str, payload: Mapping[str, Any],
             result: Mapping[str, Any]) -> str:
    directory = root / "runs" / "action-receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{int(time.time() * 1000)}-{operation}.json"
    path.write_text(json.dumps({
        "schema": "mindie-agent/rsi-action-receipt/v1",
        "operation": operation,
        "payload": dict(payload),
        "result": dict(result),
        "recorded_unix": time.time(),
    }, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    return str(path)


def _parse(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return dict(value)
    return {}


def _file_sha256(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _run(root: Path, operation: str, payload: Mapping[str, Any],
         binding: Mapping[str, Any], command: Any) -> dict[str, Any]:
    argv = _argv(command, f"{operation}_cmd")
    if argv is None:
        return {"status": "unavailable", "passed": False,
                "reason": f"{operation}_command_not_configured"}
    timeout = payload.get("timeout_s", binding.get("timeout_s", 3600))
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout_s must be a number")
    timeout = max(1, min(int(timeout), 86400))
    cwd = payload.get("cwd", binding.get("cwd", str(root)))
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("cwd must be a non-empty string")
    try:
        process = subprocess.run(
            argv,
            input=json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env=_environment(binding, payload),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"status": "unknown", "passed": False,
                "reason": f"{operation}_timeout", "timeout_s": timeout,
                "error": str(exc)}
    except OSError as exc:
        return {"status": "infra_failed", "passed": False, "reason": str(exc)}
    result = _parse(process.stdout or "")
    if not result:
        result = {"stdout": (process.stdout or "")[-12000:]}
    result.setdefault("status", "completed" if process.returncode == 0 else "failed")
    result.setdefault("passed", process.returncode == 0)
    result["returncode"] = process.returncode
    if process.stderr:
        result["stderr"] = process.stderr[-12000:]
    return result


def _run_performance_protocol(root: Path, payload: Mapping[str, Any],
                              binding: Mapping[str, Any], command: Any) -> dict[str, Any]:
    manifest = payload.get("workload_manifest")
    if not isinstance(manifest, Mapping):
        return _run(root, "performance", payload, binding, command)
    try:
        warmups = max(0, int(manifest.get("warmup_runs", 1) or 0))
        repeats = max(1, int(manifest.get("repeats", 1) or 1))
    except (TypeError, ValueError):
        return {"status": "failed", "passed": False, "reason": "invalid_workload_repeat_policy"}
    for index in range(warmups):
        warmup = _run(root, "performance", {**payload, "workload_phase": "warmup",
                                              "run_index": index}, binding, command)
        if warmup.get("status") in {"failed", "unknown", "infra_failed"} or warmup.get("passed") is False:
            return {"status": "failed", "passed": False, "reason": "warmup_failed",
                    "warmup": warmup, "run_index": index}
    measured = []
    for index in range(repeats):
        result = _run(root, "performance", {**payload, "workload_phase": "measured",
                                             "run_index": index}, binding, command)
        if result.get("status") in {"failed", "unknown", "infra_failed"} or result.get("passed") is False:
            return {"status": result.get("status", "failed"), "passed": False,
                    "reason": "measured_run_failed", "runs": measured, "failed_run": result}
        measured.append(result)
    merged = dict(measured[-1])
    merged["warmup_runs"] = warmups
    merged["repeats"] = repeats
    merged["runs"] = measured
    for field in ("per_request", "paired_runs", "baseline_samples", "candidate_samples"):
        rows = [row for result in measured for row in (result.get(field) or [])
                if isinstance(row, Mapping)]
        if field == "per_request":
            try:
                from .rsi_workload import request_record
            except ImportError:
                from rsi_workload import request_record
            rows = [request_record(row, run_index=index, phase="measured")
                    for index, result in enumerate(measured)
                    for row in (result.get(field) or []) if isinstance(row, Mapping)]
        if rows:
            merged[field] = rows
    return merged


def _normalize_profile(result: dict[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("status") in {"failed", "unknown", "infra_failed"}:
        return result
    paths = result.get("profile_paths") or result.get("msprof_paths") or payload.get("profile_paths")
    if not isinstance(paths, (list, tuple)):
        return result
    try:
        from .rsi_profiler_ascend import parse
    except ImportError:
        from rsi_profiler_ascend import parse
    normalized = parse(paths)
    result.update(normalized)
    stages = normalized.get("stages", {})
    result.setdefault("profile_observations", [{
        "elapsed_s": sum(float(value or 0.0) for value in stages.values()),
        "completion_tokens": payload.get("completion_tokens", 0),
        "draft_s": stages.get("draft", 0.0),
        "verify_s": stages.get("verify", 0.0),
        "scheduler_s": stages.get("scheduler", 0.0),
        "memory_s": stages.get("memory", 0.0),
    }])
    return result


def dispatch(operation: str, root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    binding = _binding(payload)
    commands = _commands(binding, payload)
    if operation == "prepare" and not any(
        _argv(commands.get(name), name) for name in COMMAND_KEYS["prepare"]
    ):
        candidate = payload.get("candidate")
        snapshot = {
            "startup_sha256": _file_sha256(payload.get("startup_script")),
            "test_script_sha256": _file_sha256(payload.get("test_script")),
            "test_config_sha256": _file_sha256(payload.get("test_config")),
        }
        startup_path = payload.get("startup_script")
        startup_value = None
        startup_source = "default"
        try:
            from .rsi_workload import DEFAULT_SPECULATIVE_LENGTH, discover_startup_parameter
        except ImportError:
            from rsi_workload import DEFAULT_SPECULATIVE_LENGTH, discover_startup_parameter
        if isinstance(startup_path, str) and Path(startup_path).is_file():
            try:
                startup_text = Path(startup_path).read_text(encoding="utf-8")
                value = discover_startup_parameter(startup_text)
                if value is not None:
                    startup_value = value
                    startup_source = "startup_script"
            except (OSError, UnicodeDecodeError):
                pass
        snapshot["startup_parameters"] = {
            "num_speculative_tokens": (startup_value if startup_value is not None
                                        else DEFAULT_SPECULATIVE_LENGTH),
            "num_speculative_tokens_source": startup_source,
        }
        return {"status": "prepared", "passed": True,
                "candidate_id": candidate.get("candidate_id") if isinstance(candidate, Mapping) else None,
                "deployment": payload.get("deployment") or binding.get("deployment") or {},
                "workload_snapshot": snapshot}
    if operation in {"status", "report"} and not any(
        _argv(commands.get(name), name) for name in COMMAND_KEYS.get(operation, ())
    ):
        return {"status": "completed", "passed": True, "runtime_root": str(root)}
    if operation == "cleanup" and not any(
        _argv(commands.get(name), name) for name in COMMAND_KEYS["cleanup"]
    ):
        return {"status": "completed", "passed": True, "cleanup": "not_configured"}
    names = COMMAND_KEYS.get(operation)
    if names is None:
        return {"status": "failed", "passed": False, "reason": f"unsupported_operation:{operation}"}
    command = next((commands[name] for name in names if name in commands), None)
    if operation == "accuracy" and command is None:
        rows = payload.get("token_forcing_rows") or payload.get("accuracy_rows")
        try:
            from .rsi_token_forcing import rows_from_sequences, score
        except ImportError:
            from rsi_token_forcing import rows_from_sequences, score
        if not isinstance(rows, list):
            candidate = payload.get("candidate_token_ids")
            stock = payload.get("stock_token_ids")
            if isinstance(candidate, list) and isinstance(stock, list):
                rows = rows_from_sequences(candidate, stock, prompt_id=payload.get("prompt_id"))
        if isinstance(rows, list):
            return score([row for row in rows if isinstance(row, Mapping)],
                         margin_delta=payload.get("margin_delta"))
    if operation == "profile" and command is None:
        paths = payload.get("profile_paths") or payload.get("msprof_paths")
        if isinstance(paths, (list, tuple)):
            try:
                from .rsi_profiler_ascend import parse
            except ImportError:
                from rsi_profiler_ascend import parse
            report = parse(paths)
            return _normalize_profile({"status": "completed", "passed": bool(paths), **report}, payload)
    result = (_run_performance_protocol(root, payload, binding, command)
              if operation == "performance" else _run(root, operation, payload, binding, command))
    if operation == "profile":
        result = _normalize_profile(result, payload)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation")
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload = _payload()
        root = args.root.resolve()
        result = dispatch(args.operation, root, payload)
        result["receipt"] = _receipt(root, args.operation, payload, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") not in {"failed", "infra_failed", "unknown", "unavailable"} else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "passed": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
