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


class ActivatedTaskClient:
    """TaskClient facade: every managed action requires the same active lease.

    Local evidence/report helpers never construct this client. Coordinator
    owns command deadlines and bounded waits; this adapter adds admission and
    the persistent failure circuit without replaying requests.
    """
    METHODS = frozenset({"status", "sources", "run", "target", "resolve_execution",
                         "observe", "finish", "wait"})

    def __init__(self, context_file, kwargs):
        from mindie_exec import _credentials, _load_gate
        _load_gate()
        from session_gate import Sessions
        from update_lock import update_lock
        from mindie_coordinator.task_client import TaskClient
        self._session, self._token = _credentials()
        self._sessions = Sessions()
        with update_lock(self._sessions.config):
            self._sessions.check(self._session, self._token)
            self._client = TaskClient(context_file, **kwargs)

    @property
    def context(self):
        self._sessions.check(self._session, self._token)
        return self._client.context

    def __getattr__(self, name):
        if name not in self.METHODS:
            raise AttributeError(name)
        def invoke(*args, **kwargs):
            import uuid
            from update_lock import update_lock
            with update_lock(self._sessions.config):
                self._sessions.check(self._session, self._token)
                identity = name + ":" + uuid.uuid4().hex
                if not self._sessions.claim(self._session, "coordinator", identity, self._token):
                    raise TaskTargetError("managed action was already attempted")
                succeeded = False
                try:
                    result = getattr(self._client, name)(*args, **kwargs)
                    succeeded = True
                    return result
                finally:
                    self._sessions.finish(self._session, self._token, succeeded)
        return invoke


def task_client(context_file: str | None = None, **kwargs: Any):
    """Bind a manually activated task to the installed coordinator."""
    explicit = context_file or os.environ.get("MINDIE_COORDINATOR_CONTEXT", "")
    try:
        return ActivatedTaskClient(explicit, kwargs)
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
