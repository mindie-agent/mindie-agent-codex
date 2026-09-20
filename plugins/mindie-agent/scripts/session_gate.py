"""Local, explicitly issued task authorization. Discovery never creates state.

The lease store itself is owned by the shared knowledge core
(``mindie_knowledge.loop.activation.Admission``) under the explicit neutral
``admission_path`` recorded at setup. Authorization persists for the same
native task until it is revoked, paused by the failure circuit, or its
project scope actually changes; there is no wall-clock expiry and no
runtime/config fingerprint.

This wrapper stays free of core imports: it binds the native task identity,
validates shapes, holds the update lock for every store operation, and
dispatches into the committed runtime generation (``admission_ops.py`` under
the configured interpreter), so an old cached entrypoint never mixes an old
script with a new interpreter or library.
"""

import json
import os
from pathlib import Path
import re

from bounded_process import run
from update_lock import update_lock

IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def config_path():
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
    """Committed generation's scripts dir recorded at install; the wrapper's
    own directory for a pre-update setup layout."""
    value = (config or {}).get("runtime_scripts")
    if isinstance(value, str) and os.path.isabs(value):
        return value
    return str(Path(__file__).parent)


def generation_env(config=None):
    """Pin a helper to this adapter config and the committed interpreter.

    PYTHONPATH is omitted so a developer checkout cannot mask the runtime
    interpreter's installed knowledge/remote-dev pins.
    """
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["MINDIE_AGENT_CONFIG"] = str(Path(config or config_path()).absolute())
    return env


class Inactive(ValueError):
    pass


class Sessions:
    def __init__(self, path=None, *, op_timeout=5.0):
        self.config = Path(path or config_path())
        self.op_timeout = op_timeout

    @property
    def path(self):
        """Neutral admission SQLite path recorded in the adapter config.

        Construction does not create the file; activation does.
        """
        try:
            value = json.loads(self.config.read_text()).get("admission_path")
        except (OSError, ValueError):
            value = None
        if isinstance(value, str) and os.path.isabs(value):
            return Path(value)
        return self.config.with_name(self.config.stem + ".admission.sqlite3")

    def _config(self):
        try:
            config = json.loads(self.config.read_text())
        except (OSError, ValueError) as exc:
            raise Inactive(f"MindIE adapter configuration is unreadable: {exc}")
        python = config.get("python")
        if not isinstance(python, str) or not python:
            raise Inactive("MindIE adapter configuration has no runtime interpreter")
        return config

    def _op(self, operation, payload):
        """One committed generation (config + scripts + interpreter) under the
        operation lock. The helper receives the operation argv and the exact
        config path so a custom Sessions(path=...) never rereads the default
        or MINDIE_AGENT_CONFIG production state.
        """
        with update_lock(self.config):
            config = self._config()
            helper = Path(runtime_scripts(config)) / "admission_ops.py"
            try:
                output = run(
                    [config["python"], str(helper), operation, str(self.config)],
                    json.dumps(payload),
                    timeout=self.op_timeout,
                    max_output=32768,
                    env=generation_env(self.config),
                )
                envelope = json.loads(output)
            except Inactive:
                raise
            except Exception as exc:
                raise Inactive(
                    f"MindIE admission is unavailable: {type(exc).__name__}: {str(exc)[:160]}"
                )
            if not isinstance(envelope, dict) or envelope.get("ok") is not True:
                detail = (
                    envelope.get("error", "invalid admission response")
                    if isinstance(envelope, dict)
                    else "invalid admission response"
                )
                raise Inactive(str(detail)[:240])
            return envelope["result"]

    def activate(self):
        """Explicitly authorize this native task; persistent until revoked.

        A healthy repeated activation keeps the same token and original
        capture boundary; a paused (failure-circuit) task is refused — recovery
        is an explicit deactivate plus activate. Native identity comes only
        from CODEX_THREAD_ID inside the helper.
        """
        return self._op(
            "activate", {"project_root": Path.cwd().resolve().as_posix()}
        )

    def check(self, session, token=None):
        """Active-task check with optional activation token; no state created."""
        if not isinstance(session, str) or not IDENTITY.fullmatch(session):
            raise Inactive("Valid MindIE session identity required")
        return self._op("check", {"session": session, "token": token})

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
                "Manual MindIE session activation required; continue without the plugin"
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
        """Record one call outcome against the task's failure circuit."""
        self._op(
            "finish", {"session": session, "token": token, "succeeded": bool(succeeded)}
        )

    def deactivate(self):
        return self._op("deactivate", {})
