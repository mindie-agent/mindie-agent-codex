"""Local, entry-bound task identity. Discovery never creates state.

[Role]
Manages entry-bound session leases by dispatching to `admission_ops.py` under
the configured runtime interpreter while holding the profile update lock.

[Invariants]
- Lease store is owned by `mindie_knowledge.loop.activation.Admission` at the
  neutral `admission_path`.
- A binding lasts until explicitly revoked or its project scope changes; no
  wall-clock expiry, failure-count pause, or runtime/config fingerprint.
- Stays free of core imports so cached entrypoints never mix old scripts with
  new libraries.

[Timeouts & Locks]
- Acquires `update_lock(config)` around store operations and runs `admission_ops.py`
  via `bounded_process.run`.

[Side-Effect Policy]
- Discovery (`status`) never creates state; `claim`/`finish`/`revoke` mutate
  only the configured admission store.
"""

import json
import os
from contextlib import nullcontext
from pathlib import Path
import re

from bounded_process import run
from capture_errors import validated_failure
from update_lock import update_lock

IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
# Private helper-dispatch pin. Not an operator ambient; ordinary inherited
# MINDIE_AGENT_CONFIG must not override an installed binding.
_DISPATCH_CONFIG = "_MINDIE_AGENT_DISPATCH_CONFIG"
_explicit_config = None


def bind_explicit_config(path=None):
    """Process-local adapter path from `--config`. None clears the override.

    Also pins MINDIE_AGENT_CONFIG for child dispatch. Does not set native
    task identity.
    """
    global _explicit_config
    if path is None:
        _explicit_config = None
        return None
    resolved = Path(path).expanduser().absolute()
    if _explicit_config is not None and resolved != _explicit_config:
        raise ValueError("this process is bound to another installation profile")
    _explicit_config = resolved
    os.environ["MINDIE_AGENT_CONFIG"] = str(resolved)
    return resolved


def runtime_binding(config):
    """Code identity only; contribution and reporting choices remain mutable."""
    return {key: config.get(key) for key in
            ("python", "runtime_scripts", "engine_config", "product_validation")}


def _dispatch_config():
    if _DISPATCH_CONFIG not in os.environ:
        return None
    value = os.environ.get(_DISPATCH_CONFIG)
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError("helper dispatch config requires an absolute path")
    return Path(value).expanduser().absolute()


def config_path():
    if _explicit_config is not None:
        return Path(_explicit_config)
    dispatched = _dispatch_config()
    if dispatched is not None:
        return dispatched
    return (
        Path(
            os.environ.get(
                "MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json"
            )
        )
        .expanduser()
        .absolute()
    )


def runtime_scripts(config):
    """Installed scripts directory recorded at setup;
    only explicit installed paths are accepted."""
    value = (config or {}).get("runtime_scripts")
    if isinstance(value, str) and os.path.isabs(value):
        return value
    raise ValueError("adapter configuration lacks an absolute runtime_scripts path")


def runtime_env(config=None, *, adapter=None):
    """Pin a helper to this adapter config and the committed interpreter.

    PYTHONPATH is omitted so a developer checkout cannot mask the runtime
    interpreter's installed knowledge/remote-dev pins.
    """
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    # All adapter helpers exchange bounded UTF-8 JSON over pipes. Python on a
    # Windows host can otherwise inherit a legacy console encoding from the
    # user's profile, which disagrees with bounded_process's UTF-8 contract.
    env["PYTHONIOENCODING"] = "utf-8"
    selected = str(Path(config or config_path()).absolute())
    from diagnostic_support import environment_context, read_environment_context
    from native_context import environment, read as read_native_context
    env.update(read_environment_context(selected) if adapter is None else environment_context(adapter))
    env.update(read_native_context(selected) if adapter is None else environment(adapter))
    env["MINDIE_AGENT_CONFIG"] = selected
    env[_DISPATCH_CONFIG] = selected
    return env


class Inactive(ValueError):
    def __init__(self, message, *, failure=None):
        super().__init__(message)
        self.failure = validated_failure(failure)


class Sessions:
    def __init__(self, path=None, *, op_timeout: float | None = 30.0, stop_adapter=None):
        self.config = Path(path or config_path())
        self.op_timeout = op_timeout
        self.stop_adapter = stop_adapter

    @property
    def path(self):
        """Neutral admission SQLite path recorded in the adapter config.

        Construction does not create the file; activation does.
        """
        try:
            value = json.loads(self.config.read_text(encoding='utf-8')).get("admission_path")
        except FileNotFoundError:
            return self.config.with_name(self.config.stem + ".admission.sqlite3")
        if isinstance(value, str) and os.path.isabs(value):
            return Path(value)
        raise ValueError("MindIE adapter configuration lacks an absolute admission_path")

    def _config(self):
        try:
            config = json.loads(self.config.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise Inactive(f"MindIE adapter configuration is unreadable: {exc}")
        python = config.get("python")
        if not isinstance(python, str) or not python:
            raise Inactive("MindIE adapter configuration has no runtime interpreter")
        return config

    def _op(self, operation, payload):
        """One installed runtime (config + scripts + interpreter) under the
        operation lock. The helper receives the operation argv and the exact
        config path so a custom Sessions(path=...) never rereads the default
        or MINDIE_AGENT_CONFIG production state.
        """
        # Stop only commits the existing capture queue. Holding the code
        # cutover lock here would lose a once-delivered native Stop event.
        with nullcontext() if operation == "stop_capture" else update_lock(self.config):
            config = self.stop_adapter if operation == "stop_capture" and self.stop_adapter is not None else self._config()
            if operation == "stop_capture":
                payload = dict(payload, _adapter=config)
            helper = Path(runtime_scripts(config)) / "admission_ops.py"
            try:
                completed = run(
                    [config["python"], str(helper), operation, str(self.config)],
                    json.dumps(payload),
                    timeout=self.op_timeout,
                    max_output=32768,
                    env=runtime_env(self.config, adapter=config),
                    allow_service=operation == "stop_capture",
                )
                envelope = json.loads(completed.stdout)
                if not isinstance(envelope, dict) or envelope.get("ok") is not True:
                    detail = (envelope.get("error", "invalid admission response")
                              if isinstance(envelope, dict) else "invalid admission response")
                    detail = str(detail)[:240]
                    if completed.cleanup:
                        detail += "; owned-process cleanup also failed; do not repeat the operation"
                    raise Inactive(detail, failure=envelope.get("failure")
                                   if isinstance(envelope, dict) else None)
                completed.checked_stdout()
            except Inactive:
                raise
            except Exception as exc:
                raise Inactive(
                    f"MindIE admission is unavailable: {type(exc).__name__}: {str(exc)[:160]}"
                )
            return envelope["result"]

    def activate(self):
        """Bind this native task internally; persistent until revoked.

        A healthy repeated binding keeps the same token and original capture
        boundary. Failure counts never pause it — no deactivate/reactivate
        cycle is ever required. Native identity comes only from
        CODEX_THREAD_ID inside the helper.
        """
        return self._op(
            "activate", {"project_root": Path.cwd().resolve().as_posix()}
        )

    def check(self, session, token=None):
        """Active-task check with optional activation token; no state created."""
        if not isinstance(session, str) or not IDENTITY.fullmatch(session):
            raise Inactive("Valid MindIE session identity required")
        return self._op("check", {"session": session, "token": token})

    def active_lease(self, session):
        """Read an existing binding, or None; authority faults stay visible."""
        if not isinstance(session, str) or not IDENTITY.fullmatch(session):
            raise Inactive("Valid MindIE session identity required")
        return self._op("active_lease", {"session": session})

    def resolve(self, token):
        """Resolve an activation token to its owning valid lease.

        Codex does not expose native task identity to arbitrary local
        processes, so the per-session activation token remains the checked
        identity on the internal runtime path. It resolves server-side to
        exactly one lease; the model never supplies a session ID and the most
        recently activated lease is never a fallback.
        """
        if not isinstance(token, str) or not token:
            raise Inactive(
                "MindIE operation requires its internal task-binding token"
            )
        return self._op("resolve", {"token": token})

    def claim(self, session, kind, identity, token=None):
        """Consume one unique attempt atomically. Failure/crash/restart never
        replays this item; any repeat returns False."""
        if not isinstance(session, str) or not IDENTITY.fullmatch(session):
            raise Inactive("Valid MindIE session identity required")
        return bool(
            self._op(
                "claim",
                {"session": session, "kind": kind, "identity": identity, "token": token},
            )["claimed"]
        )

    def finish(self, session, token, succeeded):
        """Record one call outcome in the task's diagnostic failure counter."""
        self._op(
            "finish", {"session": session, "token": token, "succeeded": bool(succeeded)}
        )

    def deactivate(self):
        return self._op("deactivate", {})
