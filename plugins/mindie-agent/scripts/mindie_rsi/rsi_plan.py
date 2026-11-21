#!/usr/bin/env python3
"""Validate and render a customer-neutral RSI execution plan.

[Role] Turns a structured intent into allow-listed MindIE RSI invocations.
[Invariants] Host, source, runtime, operation, and gate fields are explicit;
             arbitrary shell fragments are rejected.
[Timeouts & Locks] Pure validation; physical NPU leases are acquired by the
                    coordinator before a rendered invocation is executed.
[Side-Effect Policy] Reads JSON and writes JSON to stdout only.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from typing import Any

try:
    from rsi_defaults import default_settings, questions
except ImportError:  # pragma: no cover - direct package execution fallback
    from .rsi_defaults import default_settings, questions

SCHEMA = "mindie-agent/rsi-plan/v1"
OPERATIONS = (
    "doctor", "status", "prepare", "build", "profile", "accuracy",
    "performance", "run", "resume", "report", "record",
    "cleanup",
)
_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_ABSOLUTE = re.compile(r"^/(?:[^/\x00]+/)*[^/\x00]+$")


class PlanError(ValueError):
    """A plan cannot be safely mapped to an RSI invocation."""


class MissingInputError(PlanError):
    """The conversational layer must collect required customer inputs."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = tuple(fields)
        super().__init__("required inputs missing: " + ", ".join(fields))


def _text(value: Any, field: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise PlanError(f"{field} must be a non-empty string")
    return value.strip()


def _argv(value: Any, field: str) -> list[str]:
    """Normalize a direct command without ever passing it through a shell."""
    if isinstance(value, str):
        if not value.strip() or any(char in value for char in ";|&><`$\n\r"):
            raise PlanError(f"{field} must be a shell-free command or argv array")
        try:
            value = shlex.split(value)
        except ValueError as exc:
            raise PlanError(f"{field} has invalid command quoting") from exc
    if not isinstance(value, list) or not value:
        raise PlanError(f"{field} must be a non-empty argv array or command string")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PlanError(f"{field} must contain non-empty strings")
    if any("\x00" in item for item in value):
        raise PlanError(f"{field} cannot contain NUL bytes")
    return [item.strip() for item in value]


def _remote_path(value: Any, field: str) -> str:
    value = _text(value, field)
    if not _ABSOLUTE.fullmatch(value) or ".." in value.split("/"):
        raise PlanError(f"{field} must be an absolute remote path without '..'")
    return value.rstrip("/") or "/"


def _int(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PlanError(f"{field} must be an integer in [{low}, {high}]")
    return value


def validate(plan: Any) -> dict[str, Any]:
    """Return a normalized plan or raise :class:`PlanError`."""
    if not isinstance(plan, dict):
        raise PlanError("plan must be an object")
    if plan.get("schema") != SCHEMA:
        raise PlanError(f"schema must be {SCHEMA}")
    task_id = _text(plan.get("task_id"), "task_id")
    if not _TASK.fullmatch(task_id):
        raise PlanError("task_id contains unsupported characters")

    endpoint = plan.get("endpoint")
    if not isinstance(endpoint, dict):
        raise PlanError("endpoint must be an object")
    host = _text(endpoint.get("host"), "endpoint.host")
    port = _int(endpoint.get("port"), "endpoint.port", 1, 65535)
    user = _text(endpoint.get("user", "root"), "endpoint.user")

    source_root = _remote_path(plan.get("source_root"), "source_root")
    rsi_root = _remote_path(plan.get("rsi_root"), "rsi_root")
    missing = []
    if plan.get("startup_script") is None:
        missing.append("startup_script")
    test_script_value = plan.get("test_script")
    test_config_value = plan.get("test_config")
    test_description_value = plan.get("test_description")
    test_command_value = plan.get("test_command")
    if all(value is None for value in (test_script_value, test_config_value,
                                       test_description_value, test_command_value)):
        missing.append("test_script or test_config or test_description or test_command")
    if missing:
        raise MissingInputError(missing)
    startup_script = _remote_path(plan.get("startup_script"), "startup_script")
    test_script = (_remote_path(test_script_value, "test_script")
                   if test_script_value is not None else None)
    test_config = (_remote_path(test_config_value, "test_config")
                   if test_config_value is not None else None)
    test_description = (_text(test_description_value, "test_description")
                        if test_description_value is not None else None)
    test_command = (_argv(test_command_value, "test_command")
                    if test_command_value is not None else None)
    baseline = _text(plan.get("baseline"), "baseline")
    objective = _text(plan.get("objective"), "objective")
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise PlanError("operations must be a non-empty array")
    if any(op not in OPERATIONS for op in operations):
        raise PlanError(f"operations must use only: {', '.join(OPERATIONS)}")
    if len(set(operations)) != len(operations):
        raise PlanError("operations must not contain duplicates")

    devices = plan.get("devices", [])
    if not isinstance(devices, list) or any(not isinstance(x, str) or not x.strip() for x in devices):
        raise PlanError("devices must be an array of non-empty strings")
    workers = _int(plan.get("parallel_workers", 1), "parallel_workers", 1, 128)
    bindings = plan.get("bindings")
    if bindings is not None:
        bindings = _remote_path(bindings, "bindings")
    payload = plan.get("payload", {})
    if not isinstance(payload, dict):
        raise PlanError("payload must be an object")
    unsafe = {k for k in payload if k in {"cmd", "command", "shell", "eval"}}
    if unsafe:
        raise PlanError("payload cannot contain shell fields: " + ", ".join(sorted(unsafe)))

    defaults = default_settings(payload)
    payload = dict(payload)
    payload.setdefault("profile_binding", defaults["profile_binding"])
    payload.setdefault("optimization_scope", defaults["optimization_scope"])
    payload.setdefault("algorithm_exploration_policy", defaults["algorithm_exploration_policy"])
    payload.setdefault("source_change_policy", defaults["source_change_policy"])
    payload["algorithm_exploration_authorized"] = False
    candidate_order = payload.get("candidate_order", defaults["candidate_order"])
    if not isinstance(candidate_order, list):
        candidate_order = list(defaults["candidate_order"])
    candidate_order = [item for item in candidate_order if item != "source_changes"]
    if not candidate_order:
        candidate_order = ["runtime_parameters", "startup_features"]
    payload["candidate_order"] = candidate_order

    normalized = {
        "schema": SCHEMA,
        "task_id": task_id,
        "endpoint": {"host": host, "port": port, "user": user},
        "source_root": source_root,
        "rsi_root": rsi_root,
        "startup_script": startup_script,
        "test_script": test_script,
        "test_config": test_config,
        "test_description": test_description,
        "test_command": test_command,
        "baseline": baseline,
        "objective": objective,
        "operations": list(operations),
        "parallel_workers": workers,
        "devices": [x.strip() for x in devices],
        "bindings": bindings,
        "payload": dict(payload),
        "defaults": defaults,
    }
    return normalized


def render(plan: Any) -> dict[str, Any]:
    """Render safe MCP-facing calls; this function never executes them."""
    normalized = validate(plan)
    common = {
        "host": normalized["endpoint"]["host"],
        "port": normalized["endpoint"]["port"],
        "user": normalized["endpoint"]["user"],
        "cwd": normalized["rsi_root"],
    }
    payload = {
        **normalized["payload"],
        "defaults": normalized["defaults"],
        "task_id": normalized["task_id"],
        "source_root": normalized["source_root"],
        "startup_script": normalized["startup_script"],
        "test_script": normalized["test_script"],
        "test_config": normalized["test_config"],
        "test_description": normalized["test_description"],
        "test_command": normalized["test_command"],
        "baseline": normalized["baseline"],
        "objective": normalized["objective"],
        "parallel_workers": normalized["parallel_workers"],
        "devices": normalized["devices"],
    }
    if normalized["bindings"]:
        payload["bindings"] = normalized["bindings"]
    payload_path = f"/tmp/mindie-rsi-{normalized['task_id']}.json"
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    steps = [{
        "operation": "write_payload",
        "tool": "remote_write",
        "arguments": {**common, "file_path": payload_path,
                       "content": payload_text, "overwrite": True},
        "requires": [],
    }]
    for operation in normalized["operations"]:
        steps.append({
            "operation": operation,
            "tool": "remote_exec_command",
            "arguments": {
                **common,
                "cmd": f"python3 bin/mindie_agent_rsi_entry.py {operation} --root {normalized['rsi_root']} < {payload_path}",
            },
            "requires": ["coordinator.lease"] if operation in {
                "run", "resume", "profile", "accuracy", "performance"
            } else [],
        })
    return {
        "schema": SCHEMA,
        "task_id": normalized["task_id"],
        "endpoint": normalized["endpoint"],
        "steps": steps,
        "gates": {
            "benchmark_passed": "performance" in normalized["operations"],
            "correctness_passed": "accuracy" in normalized["operations"],
            "teacher_forced": bool(normalized["payload"].get("teacher_forced", False)),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "render", "questions"))
    parser.add_argument("plan", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if args.command == "questions":
            payload = plan.get("payload", {}) if isinstance(plan, dict) else {}
            result = {"questions": questions(payload),
                      "defaults": default_settings(payload)}
            if result["questions"]:
                result["status"] = "needs_input"
            else:
                result["status"] = "valid"
        else:
            result = validate(plan) if args.command == "validate" else render(plan)
    except (OSError, json.JSONDecodeError, MissingInputError) as exc:
        fields = list(exc.fields) if isinstance(exc, MissingInputError) else []
        print(json.dumps({"schema": SCHEMA, "status": "needs_input",
                          "required_inputs": fields, "error": str(exc)}, ensure_ascii=False))
        return 2
    except PlanError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.command == "questions":
        print(json.dumps({"schema": SCHEMA, **result}, ensure_ascii=False))
    else:
        print(json.dumps({"schema": SCHEMA, "status": "valid", "plan": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
