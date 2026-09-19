"""Remote execution over the pinned remote-dev package; no copied SSH layer.

Domain tools call these helpers with an explicit endpoint. When the MindIE
adapter is configured on this machine, callers must present the current task's
activation (MINDIE_SESSION_ID + MINDIE_ACTIVATION, exported by the entry
skill's activation step); the lease is verified against the local session gate
before any remote call. Without any adapter configuration the tools run as
plain development CLIs. Long work runs as a remote job whose job ID is returned
to the caller; artifacts move through the remote-dev artifact tools with hash
verification handled there.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
from typing import Any

from mindie_target import SshEndpoint, endpoint_args

DEFAULT_CONNECT_TIMEOUT_MS = 10000
DEFAULT_YIELD_MS = 30000


class RemoteExecutionError(RuntimeError):
    pass


def _gate() -> str:
    """Return the per-run remote-dev session id, enforcing plugin admission."""
    config = os.environ.get("MINDIE_AGENT_CONFIG")
    candidate = Path(config) if config else Path.home() / ".config/mindie-agent/codex.json"
    session = os.environ.get("MINDIE_SESSION_ID", "")
    if not candidate.exists():
        # No adapter configured: plain development CLI use.
        return session or f"mindie-cli-{os.getpid()}"
    if not session or not os.environ.get("MINDIE_ACTIVATION"):
        raise RemoteExecutionError(
            "manual MindIE activation required: run the entry skill's activate step "
            "and export MINDIE_SESSION_ID/MINDIE_ACTIVATION for domain CLIs"
        )
    scripts = None
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "session_gate.py").is_file():
            scripts = parent / "scripts"
            break
    if scripts is None:
        raise RemoteExecutionError("MindIE session gate not found in the installed plugin")
    import sys

    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from session_gate import Sessions

    try:
        Sessions().check(session, os.environ["MINDIE_ACTIVATION"])
    except Exception as exc:
        raise RemoteExecutionError(f"MindIE session not admitted: {exc}") from exc
    return session


def _state_env(session: str) -> None:
    os.environ["REMOTE_DEV_STATE_DIR"] = os.environ.get(
        "MINDIE_REMOTE_STATE_DIR",
        str(Path.home() / ".local/share/mindie-agent/remote-dev"),
    )
    os.environ["REMOTE_DEV_SESSION_ID"] = session


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    session = _gate()
    _state_env(session)
    from remote_dev.core.rpc_transport import close_connections
    from remote_dev.mcp.tools import call_tool

    try:
        value = call_tool(name, args)
    finally:
        close_connections()
    result = value.get("result", {})
    if result.get("outcome") not in {"success", "cancelled"}:
        raise RemoteExecutionError(f"{name} failed: {result!r}"[:400])
    return result


def _endpoint_args(endpoint: SshEndpoint, container: str | None, cwd: str | None):
    args = endpoint_args(endpoint)
    args["connect_timeout_ms"] = DEFAULT_CONNECT_TIMEOUT_MS
    if container:
        args["container"] = container
    if cwd:
        args["cwd"] = cwd
    return args


def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    timeout: float | None = 180,
    connect_timeout: float = 10,
    container: str | None = None,
    cwd: str | None = None,
) -> str:
    """Run one bounded remote command and return its captured output.

    Long-running work must not use this: start a job and keep its job ID.
    """
    args = _endpoint_args(endpoint, container, cwd)
    args.update(
        command=script,
        timeout_ms=int((timeout or 180) * 1000),
        connect_timeout_ms=int(connect_timeout * 1000),
        yield_time_ms=min(int((timeout or 180) * 1000), DEFAULT_YIELD_MS),
    )
    try:
        result = _call("remote_bash", args)
    except RemoteExecutionError:
        if check:
            raise
        return ""
    job = result.get("job_id")
    if job:
        raise RemoteExecutionError(
            f"command outlived the bounded window and continues as remote job {job}; "
            "poll it with job_status/job_tail, do not rerun blindly"
        )
    return str(result.get("output", ""))


def ssh_run_bytes(endpoint: SshEndpoint, script: str, *, timeout: float = 180) -> bytes:
    return ssh_exec(endpoint, script, timeout=timeout).encode()


def ssh_argv(
    endpoint: SshEndpoint,
    *,
    long_stream: bool = False,
    connect_timeout_s: float | None = None,
    identity_file: str | None = None,
    ssh_mux: bool | None = None,
) -> list[str]:
    """Compose the SSH base argv for an endpoint via the remote-dev transport.

    Argv composition performs no remote access, so it is not admission-gated;
    executing the command through ssh_exec/jobs/forwards is.
    """
    from dataclasses import replace
    from remote_dev.core.ssh_transport import ssh_base_cmd

    ep = endpoint
    changes: dict[str, Any] = {}
    if connect_timeout_s is not None:
        changes["connect_timeout_ms"] = int(connect_timeout_s * 1000)
    if identity_file is not None:
        changes["identity_file"] = identity_file
    if ssh_mux is not None:
        changes["ssh_mux"] = ssh_mux
    if changes:
        ep = replace(ep, **changes)
    if long_stream:
        ep = SshEndpoint.for_long_stream(
            ep.host,
            ep.port,
            **{
                key: getattr(ep, key)
                for key in ("user", "identity_file", "connect_timeout_ms", "root", "cwd")
                if getattr(ep, key, None) not in (None, "")
            },
        )
    return list(ssh_base_cmd(ep))


def shell_quote(*parts: str) -> str:
    return " ".join(shlex.quote(str(p)) for p in parts)


def ssh_stream(endpoint: SshEndpoint, script: str, *, timeout: float = 180):
    """Yield remote output; remote-dev is request/reply, so this yields the
    bounded output of one call as a single chunk."""
    yield ssh_exec(endpoint, script, timeout=timeout)


def open_local_forward(endpoint: SshEndpoint, remote_port: int, **kwargs):
    """Local→remote SSH forward via the remote-dev transport; returns its handle."""
    _gate()
    from remote_dev.core.ssh_transport import open_local_forward as _open

    return _open(endpoint, remote_port, **kwargs)


def require_transport():
    from remote_dev.mcp.tools import call_tool  # noqa: F401


def start_job(
    endpoint: SshEndpoint,
    command: str,
    *,
    name: str = "mindie-job",
    container: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    args = _endpoint_args(endpoint, container, cwd)
    args.update(command=command, description=name, tty=False, yield_time_ms=1000)
    if env:
        args["env"] = env
    result = _call("remote_bash", args)
    job = result.get("job_id") or result.get("job") or ""
    if not job:
        raise RemoteExecutionError(f"remote job did not return an id: {result!r}"[:400])
    return str(job)


def job_status(endpoint: SshEndpoint, job_id: str, *, container=None, cwd=None) -> dict[str, Any]:
    args = _endpoint_args(endpoint, container, cwd)
    args["job_id"] = job_id
    return _call("remote_job_status", args)


def job_tail(endpoint: SshEndpoint, job_id: str, *, lines: int = 200, container=None, cwd=None) -> str:
    args = _endpoint_args(endpoint, container, cwd)
    args.update(job_id=job_id, lines=lines)
    return str(_call("remote_job_tail", args).get("output", ""))


def job_stop(endpoint: SshEndpoint, job_id: str, *, force: bool = False, container=None, cwd=None) -> None:
    args = _endpoint_args(endpoint, container, cwd)
    args.update(job_id=job_id, force=force)
    _call("remote_job_stop", args)


def artifact_pull(endpoint: SshEndpoint, remote_path: str, local_dir: str, *, container=None, cwd=None) -> dict[str, Any]:
    args = _endpoint_args(endpoint, container, cwd)
    args.update(remote_path=remote_path, local_dir=local_dir)
    return _call("remote_artifact_pull", args)


def artifact_push(endpoint: SshEndpoint, local_path: str, remote_path: str, *, container=None, cwd=None) -> dict[str, Any]:
    args = _endpoint_args(endpoint, container, cwd)
    args.update(local_path=local_path, remote_path=remote_path)
    return _call("remote_artifact_push", args)


def pid_alive(pid: int) -> bool:
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    # Windows (unverified on real hardware).
    import subprocess

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
