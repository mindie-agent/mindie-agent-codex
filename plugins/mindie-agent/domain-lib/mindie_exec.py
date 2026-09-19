"""Remote execution through the MindIE Gate and pinned remote-dev package.

Domain tools call these helpers with an explicit endpoint. Every remote
execution or SSH forward requires a current manual activation against the
adapter config: missing config, missing lease, and Gate failures all fail
closed. Calls reuse Gate claim/finish, update_lock, and bounded_process
(65s remote budget, 1MiB output, cancel). They do not talk to remote-dev
beside runtime_call.py, so MCP-started jobs share REMOTE_DEV_SESSION_ID
and REMOTE_DEV_STATE_DIR with Skill-side job_status/job_tail/job_stop.

Completion of remote_bash is state/status plus exit_code; preview.stdout /
preview.stderr hold output. job_id is always a reference, including after
the command has finished. Long work launches once via start_job.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import math
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any
import uuid

from mindie_target import SshEndpoint, endpoint_args

DEFAULT_CONNECT_TIMEOUT_MS = 10000
DEFAULT_YIELD_MS = 30000
REMOTE_CALL_BUDGET_SECONDS = 65
ARTIFACT_DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_DRAIN_CHARS = 1024 * 1024
TERMINAL_STATES = {"succeeded", "failed", "timeout", "cancelled"}
UNKNOWN_STATES = {"unknown", "absent", "lost"}


class RemoteExecutionError(RuntimeError):
    """Infrastructure, admission, or incomplete-observation failure.

    Remote command non-zero exits are subprocess.CalledProcessError when
    check=True, or a CompletedProcess when check=False. This error is never
    used to swallow authentication or transport failures into empty output.
    """

    def __init__(self, message: str, *, job_id: str | None = None):
        super().__init__(message)
        self.job_id = job_id


def _scripts_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        scripts = parent / "scripts"
        if (scripts / "mcp_gate.py").is_file():
            return scripts
    raise RemoteExecutionError("MindIE session gate not found in the installed plugin")


def _load_gate():
    scripts = _scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import mcp_gate

    return mcp_gate


def _credentials() -> tuple[str, str]:
    config = Path(
        os.environ.get(
            "MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json"
        )
    ).expanduser()
    if not config.is_file():
        raise RemoteExecutionError(
            "MindIE adapter configuration is required; remote execution fails closed"
        )
    session = os.environ.get("MINDIE_SESSION_ID", "")
    token = os.environ.get("MINDIE_ACTIVATION", "")
    if not session or not token:
        raise RemoteExecutionError(
            "manual MindIE activation required: run the entry skill's activate step "
            "and export MINDIE_SESSION_ID/MINDIE_ACTIVATION for domain CLIs"
        )
    return session, token


def _error_text(response: dict[str, Any]) -> str:
    parts = []
    for item in response.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "".join(parts)[:400] or "MindIE remote call failed; no retry"


def _local_budget(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    return min(max(0.01, float(timeout)), REMOTE_CALL_BUDGET_SECONDS)


def _invoke(name: str, args: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """One Gate-admitted remote-dev tool call. Does not create a second transport."""
    session, token = _credentials()
    mcp_gate = _load_gate()
    request = {
        "id": f"cli-{uuid.uuid4().hex}",
        "params": {
            "name": name,
            "arguments": {
                **args,
                "mindie_session_id": session,
                "mindie_activation": token,
            },
        },
    }
    response = mcp_gate.Gate("remote").call(request, timeout=_local_budget(timeout))
    result = response.get("structuredContent")
    if not isinstance(result, dict):
        raise RemoteExecutionError(_error_text(response))
    details = result.get("error_details") if isinstance(result.get("error_details"), dict) else {}
    if (
        result.get("status") == "submission_uncertain"
        or details.get("submission_state") == "uncertain"
    ):
        raise RemoteExecutionError(
            f"{name} submission outcome is unknown; observe the original job, do not retry",
            job_id=str(result.get("job_id") or result.get("session_id") or "") or None,
        )
    outcome = result.get("outcome")
    if response.get("isError") is True or outcome == "blocked":
        raise RemoteExecutionError(
            f"{name} failed: {_error_text(response)}"[:400],
            job_id=_job_id_of(result) or None,
        )
    return result


def _endpoint_args(endpoint: SshEndpoint, container: str | None, cwd: str | None):
    args = endpoint_args(endpoint)
    args["connect_timeout_ms"] = DEFAULT_CONNECT_TIMEOUT_MS
    if container:
        args["container"] = container
    if cwd:
        args["cwd"] = cwd
    return args


def _job_id_of(result: dict[str, Any]) -> str:
    return str(result.get("job_id") or result.get("session_id") or "")


def _state_of(result: dict[str, Any]) -> str:
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    remote = job.get("remote_status") if isinstance(job.get("remote_status"), dict) else {}
    for candidate in (
        result.get("state"),
        result.get("status"),
        remote.get("state"),
        job.get("state"),
    ):
        if candidate:
            return str(candidate)
    return ""


def _preview_streams(result: dict[str, Any]) -> tuple[str, str]:
    preview = result.get("preview") if isinstance(result.get("preview"), dict) else {}
    stdout = preview.get("stdout")
    stderr = preview.get("stderr")
    if stdout is None and "tail" in preview:
        stdout = preview.get("tail")
    return str(stdout or ""), str(stderr or "")


def _bytes_remaining(result: dict[str, Any]) -> int:
    pending = result.get("bytes_remaining") if isinstance(result.get("bytes_remaining"), dict) else {}
    return int(pending.get("stdout") or 0) + int(pending.get("stderr") or 0)


def _exit_code_of(result: dict[str, Any]) -> int | None:
    value = result.get("exit_code")
    if isinstance(value, int):
        return value
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    remote = job.get("remote_status") if isinstance(job.get("remote_status"), dict) else {}
    nested = remote.get("result") if isinstance(remote.get("result"), dict) else {}
    for candidate in (nested.get("exit_code"), remote.get("exit_code")):
        if isinstance(candidate, int):
            return candidate
    return None


def _require_positive_timeout(timeout: float | None, label: str) -> float:
    if timeout is None or not math.isfinite(float(timeout)) or float(timeout) <= 0:
        raise RemoteExecutionError(f"{label} requires a positive timeout")
    return float(timeout)


def _stop_owned(job_id: str | None) -> None:
    if not job_id:
        return
    try:
        _invoke("remote_job_stop", {"job_id": job_id, "force": True})
    except Exception:
        return


def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    timeout: float | None = 180,
    connect_timeout: float = 10,
    container: str | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded remote command and return a CompletedProcess.

    ``timeout`` is the remote deadline. The local Gate call is still capped
    at 65s; a command that outlives the yield window is observed through the
    returned job_id (poll, never relaunch). ``check=False`` only tolerates a
    completed remote command with a non-zero exit_code.
    """
    remote_timeout = _require_positive_timeout(timeout, "ssh_exec")
    timeout_ms = int(remote_timeout * 1000)
    args = _endpoint_args(endpoint, container, cwd)
    args.update(
        command=script,
        timeout_ms=timeout_ms,
        connect_timeout_ms=min(DEFAULT_CONNECT_TIMEOUT_MS, int(connect_timeout * 1000)),
        yield_time_ms=min(timeout_ms, DEFAULT_YIELD_MS),
        max_output_tokens=16384,
        description="mindie-ssh-exec",
        tty=False,
    )
    deadline = time.monotonic() + remote_timeout
    observations = 0
    job_id = None
    stdout = stderr = ""
    result: dict[str, Any] = {}
    try:
        result = _invoke("remote_bash", args, timeout=remote_timeout)
        job_id = _job_id_of(result)
        stdout, stderr = _preview_streams(result)
        state = _state_of(result)
        while True:
            if len(stdout.encode()) + len(stderr.encode()) > MAX_DRAIN_CHARS:
                raise RemoteExecutionError("remote output exceeds the local bound", job_id=job_id)
            if state in UNKNOWN_STATES:
                raise RemoteExecutionError(
                    f"remote job {job_id or '?'} outcome is {state}; not retried",
                    job_id=job_id or None,
                )
            remaining = _bytes_remaining(result)
            if state in TERMINAL_STATES and remaining <= 0:
                break
            if not job_id:
                raise RemoteExecutionError(
                    f"remote command did not complete and returned no job_id: {result!r}"[:400]
                )
            if time.monotonic() >= deadline or observations >= 128:
                raise RemoteExecutionError(
                    f"command still {state or 'running'} as remote job {job_id}; "
                    "poll it with job_status/job_tail, do not relaunch",
                    job_id=job_id,
                )
            if len(stdout) + len(stderr) > MAX_DRAIN_CHARS:
                raise RemoteExecutionError(
                    f"remote job {job_id} output exceeds the local bound; partial preview discarded",
                    job_id=job_id,
                )
            observations += 1
            result = _invoke(
                "remote_job_stdin",
                {
                    "job_id": job_id,
                    "chars": "",
                    "yield_time_ms": min(DEFAULT_YIELD_MS, max(1000, timeout_ms)),
                    "max_output_tokens": 16384,
                },
                timeout=max(0.01, deadline - time.monotonic()),
            )
            more_out, more_err = _preview_streams(result)
            stdout += more_out
            stderr += more_err
            state = _state_of(result) or state
            leftover = _bytes_remaining(result)
            if state in TERMINAL_STATES and leftover > 0 and not more_out and not more_err:
                raise RemoteExecutionError(
                    f"remote job {job_id} completed with {leftover} unread bytes; "
                    "refusing to treat a truncated preview as the full result",
                    job_id=job_id,
                )
        exit_code = _exit_code_of(result)
        if exit_code is None:
            raise RemoteExecutionError(
                f"completed remote command missing exit_code (state={state}, job={job_id})",
                job_id=job_id or None,
            )
        proc = subprocess.CompletedProcess([script], int(exit_code), stdout, stderr)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, script, proc.stdout, proc.stderr
            )
        return proc
    except RemoteExecutionError:
        if job_id and _state_of(result) in {"running", "starting", "queued", "waiting", "created"}:
            _stop_owned(job_id)
        raise
    except subprocess.CalledProcessError:
        raise
    except BaseException:
        _stop_owned(job_id)
        raise


def shell_quote(*parts: str) -> str:
    return " ".join(shlex.quote(str(p)) for p in parts)


@contextmanager
def open_local_forward(endpoint: SshEndpoint, remote_port: int, **kwargs):
    """Admitted local→remote SSH forward; the owned process is always closed.

    The tunnel is a local owned process, not a runtime_call child, so it can
    outlive a single 65s Gate budget. It cannot be detached: leaving the
    context manager closes the forward. Missing config or activation fails
    closed before the process starts.
    """
    session, token = _credentials()
    _load_gate()
    from session_gate import Inactive, Sessions
    from update_lock import update_lock

    sessions = Sessions()
    identity = f"forward:{os.getpid()}:{uuid.uuid4().hex}"
    handle = None
    admitted = False
    succeeded = False
    try:
        with update_lock(sessions.config):
            try:
                sessions.check(session, token)
            except Inactive as exc:
                raise RemoteExecutionError(f"MindIE session not admitted: {exc}") from exc
            if not sessions.claim(session, "mcp", identity, token):
                raise RemoteExecutionError("Duplicate forward claim; not executed again")
            admitted = True
        from remote_dev.core.ssh_transport import open_local_forward as _open

        handle = _open(endpoint, remote_port, **kwargs)
        yield handle
        succeeded = True
    finally:
        if handle is not None:
            closer = getattr(handle, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
            owner = getattr(handle, "owner", None)
            process = getattr(owner, "process", owner)
            if process is not None and getattr(process, "poll", None) and process.poll() is None:
                killer = getattr(process, "kill", None)
                if callable(killer):
                    try:
                        killer()
                    except Exception:
                        pass
        if admitted:
            try:
                sessions.finish(session, token, succeeded)
            except Exception:
                pass


def require_transport():
    """Import remote-dev so callers fail fast when the pin is missing."""
    from remote_dev.mcp.tools import call_tool  # noqa: F401


def start_job(
    endpoint: SshEndpoint,
    command: str,
    *,
    name: str,
    timeout: float,
    container: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Launch one remote job whose deadline is ``timeout`` seconds.

    Returns job_id. A completed command still has a job_id. Does not catch
    TypeError and relaunch. ``timeout`` must be positive.
    """
    remote_timeout = _require_positive_timeout(timeout, "start_job")
    args = _endpoint_args(endpoint, container, cwd)
    args.update(
        command=command,
        description=str(name),
        timeout_ms=int(remote_timeout * 1000),
        yield_time_ms=1000,
        tty=False,
    )
    if env:
        args["env"] = env
    result = _invoke("remote_bash", args)
    if _state_of(result) in UNKNOWN_STATES:
        raise RemoteExecutionError(
            f"remote job launch outcome is unknown; not retried: {result!r}"[:400],
            job_id=_job_id_of(result) or None,
        )
    job = _job_id_of(result)
    if not job:
        raise RemoteExecutionError(f"remote job did not return an id: {result!r}"[:400])
    return job


def job_status(
    endpoint: SshEndpoint,
    job_id: str,
    *,
    timeout: float | None = None,
    container=None,
    cwd=None,
) -> dict[str, Any]:
    """Observe one job. outcome success means the RPC succeeded, not the job."""
    args = _endpoint_args(endpoint, container, cwd)
    args["job_id"] = job_id
    result = _invoke("remote_job_status", args, timeout=timeout)
    if _state_of(result) in UNKNOWN_STATES and result.get("outcome") != "success":
        raise RemoteExecutionError(
            f"remote job {job_id} status outcome is unknown; not retried",
            job_id=job_id,
        )
    return result


def job_tail(
    endpoint: SshEndpoint,
    job_id: str,
    *,
    lines: int = 200,
    timeout: float | None = None,
    container=None,
    cwd=None,
) -> dict[str, Any]:
    """Return remote_job_tail preview (stdout/stderr or tail), not a fake output field."""
    args = _endpoint_args(endpoint, container, cwd)
    args.update(job_id=job_id, lines=int(lines))
    result = _invoke("remote_job_tail", args, timeout=timeout)
    preview = result.get("preview")
    if not isinstance(preview, dict):
        raise RemoteExecutionError(
            f"remote job {job_id} tail returned no preview",
            job_id=job_id,
        )
    return preview


def job_stop(
    endpoint: SshEndpoint,
    job_id: str,
    *,
    force: bool = False,
    timeout: float | None = None,
    container=None,
    cwd=None,
) -> dict[str, Any]:
    """Stop an owned job. ``force`` is the remote-dev schema flag. Unknown is not retried."""
    args = _endpoint_args(endpoint, container, cwd)
    args.update(job_id=job_id, force=bool(force))
    result = _invoke("remote_job_stop", args, timeout=timeout)
    if _state_of(result) in UNKNOWN_STATES and result.get("outcome") not in {
        "success",
        "cancelled",
    }:
        raise RemoteExecutionError(
            f"remote job {job_id} stop outcome is unknown; not retried",
            job_id=job_id,
        )
    return result


def artifact_pull(
    endpoint: SshEndpoint,
    remote_path: str,
    local_dir: str,
    *,
    timeout: float = ARTIFACT_DEFAULT_TIMEOUT_SECONDS,
    container=None,
    cwd=None,
) -> dict[str, Any]:
    remote_timeout = _require_positive_timeout(timeout, "artifact_pull")
    args = _endpoint_args(endpoint, container, cwd)
    args.update(
        remote_path=remote_path,
        local_dir=local_dir,
        timeout_ms=int(remote_timeout * 1000),
    )
    result = _invoke("remote_artifact_pull", args, timeout=remote_timeout)
    artifacts = result.get("artifacts")
    if result.get("outcome") != "success" or not artifacts:
        raise RemoteExecutionError(f"artifact_pull failed: {result!r}"[:400])
    return result


def artifact_push(
    endpoint: SshEndpoint,
    local_path: str,
    remote_path: str,
    *,
    timeout: float = ARTIFACT_DEFAULT_TIMEOUT_SECONDS,
    container=None,
    cwd=None,
) -> dict[str, Any]:
    remote_timeout = _require_positive_timeout(timeout, "artifact_push")
    args = _endpoint_args(endpoint, container, cwd)
    args.update(
        local_path=local_path,
        remote_path=remote_path,
        timeout_ms=int(remote_timeout * 1000),
    )
    result = _invoke("remote_artifact_push", args, timeout=remote_timeout)
    artifacts = result.get("artifacts")
    if result.get("outcome") != "success" or not artifacts:
        raise RemoteExecutionError(f"artifact_push failed: {result!r}"[:400])
    return result


def pid_alive(pid: int) -> bool:
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    # Windows (unverified on real hardware).
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=10
    )
    return str(pid) in result.stdout


class owned_process:
    """Context manager: terminate a local child on exit (platform bounded)."""

    def __init__(self, process):
        self.process = process

    def __enter__(self):
        return self.process

    def __exit__(self, *exc):
        if self.process.poll() is None:
            self.process.kill()
        return False


def process_identity() -> str:
    """Stable identity string for one local process (pid-based)."""
    return f"pid-{os.getpid()}"
