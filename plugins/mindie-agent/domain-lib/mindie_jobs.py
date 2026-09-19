"""Domain adapter over the real ``mindie_coordinator`` package.

The coordinator owns managed admission, placement, leases and execution state.
This adapter only binds the current native task context (or an explicit
``--context-file``) and forwards domain resource/environment requests. No
workspace owner, session wrapper ledger or startup hook is involved.
"""

from __future__ import annotations

import os
import re
from typing import Any

DONE = frozenset({"succeeded", "failed", "timeout", "cancelled", "inconclusive"})
PENDING = frozenset(
    {
        "waiting_for_runtime",
        "queued",
        "waiting",
        "preparing",
        "starting",
        "uncertain",
        "planned",
        "launch_pending",
        "bound",
        "blocked",
    }
)
RUNNING = frozenset({"running", "active"})
# The coordinator injects these at launch; callers must not set them.
RESERVED_LAUNCH_ENV = frozenset(
    {
        "MINDIE_SERVICE_PORT",
        "ASCEND_RT_VISIBLE_DEVICES",
        "MINDIE_PYTHON",
        "MINDIE_EXECUTION_OBSERVATION",
    }
)


class TaskTargetError(RuntimeError):
    """Missing native context or coordinator refusal."""


def task_client(context_file: str | None = None, **kwargs: Any):
    """Bind the real coordinator TaskClient to this native task context.

    The context file comes from the explicit argument, then
    ``MINDIE_COORDINATOR_CONTEXT``, then the coordinator's own native-context
    resolution. No ID is guessed from directories or history.
    """
    from mindie_coordinator.task_client import TaskClient

    explicit = context_file or os.environ.get("MINDIE_COORDINATOR_CONTEXT", "")
    try:
        return TaskClient(explicit, **kwargs)
    except (ValueError, RuntimeError, OSError) as exc:
        raise TaskTargetError(str(exc)) from exc


def task_id_of(client: Any) -> str:
    return str(client.context["session"]["id"])


def reject_reserved_env(env: dict[str, str] | None) -> dict[str, str]:
    cleaned = dict(env or {})
    reserved = sorted(key for key in cleaned if key in RESERVED_LAUNCH_ENV)
    if reserved:
        raise TaskTargetError(
            "do not set reserved launch environment "
            + ", ".join(reserved)
            + "; the coordinator injects MINDIE_PYTHON, MINDIE_SERVICE_PORT, and "
            "ASCEND_RT_VISIBLE_DEVICES from the selected environment"
        )
    return cleaned


def service_resources(
    *,
    npu_count: int | None = None,
    devices: list[int] | None = None,
    service_port: int | None = 0,
    allow_external_busy: bool = False,
) -> dict[str, Any]:
    """Preserve explicit resource requests for TaskClient.run validation."""
    resources: dict[str, Any] = {}
    if devices is not None:
        resources["devices"] = list(devices)
    if npu_count is not None:
        resources["npu_count"] = npu_count
    elif devices is None:
        resources["npu_count"] = 1
    if service_port is not None:
        resources["service_port"] = service_port
    if allow_external_busy:
        resources["allow_external_busy"] = True
    return resources


ENVIRONMENT_CONSTRAINT_KEYS = ("recipe", "python_abi", "cann", "soc", "machine_type")


def named_environment(
    *,
    recipe: str | None = None,
    python_abi: str | None = None,
    cann: str | None = None,
    soc: str | None = None,
    machine_type: str | None = None,
    preset: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Forward environment constraints to TaskClient.run. Recipe is optional.

    Coordinator matching never silently ignores supplied constraints. This
    helper must not drop ``soc`` / ``machine_type`` / ABI / CANN when recipe
    is omitted.
    """
    raw: dict[str, Any] = {}
    preset_env = (preset or {}).get("environment")
    if isinstance(preset_env, dict):
        raw.update(preset_env)
    if preset:
        for key in ENVIRONMENT_CONSTRAINT_KEYS:
            if preset.get(key) not in (None, "") and key not in raw:
                raw[key] = preset[key]
    if extra:
        raw.update({key: value for key, value in extra.items() if value not in (None, "")})
    overrides = {
        "recipe": recipe,
        "python_abi": python_abi,
        "cann": cann,
        "soc": soc,
        "machine_type": machine_type,
    }
    for key, value in overrides.items():
        if value not in (None, ""):
            raw[key] = value
    out = {str(key): str(value) for key, value in raw.items() if value not in (None, "")}
    return out or None


def run_command(client: Any, command: str, **kwargs: Any) -> dict[str, Any]:
    """Submit one managed command. Association and recovery stay in the package."""
    return client.run(command, **kwargs)


def execution_target(client: Any, execution_id: str) -> dict[str, Any]:
    """Authoritative routing for one owned execution. No guessed host."""
    return client.target(execution_id)


def valid_execution_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value or ""):
        raise TaskTargetError("invalid execution id")
    return value
