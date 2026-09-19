#!/usr/bin/env python3
"""Shared utilities for ascend-profiling-collection scripts.

This module owns helpers that exist *because of profiling*: the SSH +
ascend-env preamble, the local SSH tunnel for sending workload requests, the
profile-control client, and progress / state-dir conventions.

Execution target decoding is reused from serving; remote-dev owns SSH primitives.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys

from pathlib import Path
from types import SimpleNamespace
from typing import Any

def _ensure_plugin_domain_lib() -> None:
    try:
        import mindie_state  # noqa: F401
        return
    except ImportError:
        pass
    plugin_root = Path(__file__).resolve().parents[3]
    domain = plugin_root / "domain-lib"
    if domain.is_dir():
        sys.path.insert(0, str(domain))
        return
    raise RuntimeError("MindIE domain-lib not found; use the installed plugin")


_ensure_plugin_domain_lib()
import threading
from datetime import datetime, timezone
from dataclasses import dataclass

ROOT = Path.cwd()  # the user's business checkout
SERVING_SCRIPTS = Path(__file__).resolve().parents[2] / "vllm-ascend-serving" / "scripts"

from mindie_receipt import progress as envelope_progress  # noqa: E402
from mindie_state import allocate_run_dir, safe_run_token, state_root  # noqa: E402
from mindie_exec import open_local_forward, require_transport, RemoteExecutionError  # noqa: E402
import mindie_exec as _mindie_exec  # noqa: E402
from mindie_target import SshEndpoint, ascend_env_preamble, ssh_endpoint_from_mapping  # noqa: E402


def _collection_state_dir():
    return state_root() / "ascend-profiling-collection" / "runs"


def ssh_exec(endpoint, script: str, *, check: bool = True, timeout: float | None = 180):
    """Short remote command; always expose .returncode/.stdout/.stderr."""
    try:
        raw = _mindie_exec.ssh_exec(endpoint, script, check=check, timeout=timeout)
    except Exception as exc:
        remote_error = getattr(_mindie_exec, "RemoteExecutionError", RuntimeError)
        if check or not isinstance(exc, remote_error):
            raise
        return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))
    if hasattr(raw, "returncode") and hasattr(raw, "stdout"):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        return SimpleNamespace(returncode=0, stdout=raw.decode("utf-8", "replace"), stderr="")
    return SimpleNamespace(returncode=0, stdout="" if raw is None else str(raw), stderr="")


def endpoint_from_reply(reply: dict[str, Any]) -> SshEndpoint:
    target = reply.get("target") if isinstance(reply.get("target"), dict) else {}
    endpoint = target.get("endpoint") if isinstance(target.get("endpoint"), dict) else reply.get("endpoint")
    if not isinstance(endpoint, dict) or not endpoint.get("host"):
        raise RuntimeError("coordinator reply has no ordinary endpoint")
    return ssh_endpoint_from_mapping(endpoint)


def service_port_of(reply: dict[str, Any]) -> int | None:
    port = reply.get("service_port")
    if port in (None, ""):
        target = reply.get("target") if isinstance(reply.get("target"), dict) else {}
        port = target.get("service_port")
        env = target.get("environment") if isinstance(target.get("environment"), dict) else {}
        if port in (None, "") and env.get("MINDIE_SERVICE_PORT"):
            port = env["MINDIE_SERVICE_PORT"]
    if port in (None, ""):
        return None
    return int(port)


@dataclass
class ExecutionTarget:
    mode: str
    alias: str
    endpoint: Any
    execution_id: str | None = None
    task_id: str | None = None
    python: str | None = None
    cwd: str | None = None
    session_id: str | None = None
    session_file: str | None = None
    service_port: int | None = None
    launch_preamble: str = ""


def resolve_execution_target(*, context_file=None, execution_id=None, host=None, port=None, user="root", service="vllm"):
    from mindie_target import SshEndpoint as Endpoint
    from mindie_jobs import execution_target, task_client, task_id_of

    if host:
        ep = Endpoint(host=host, port=int(port or 22), user=user)
        return ExecutionTarget(mode="endpoint", alias=host, endpoint=ep)
    client = task_client(context_file)
    if not execution_id:
        execution_id = client.resolve_execution(service=service)
        if not execution_id:
            raise RuntimeError("pass --execution-id or --host")
    target = execution_target(client, str(execution_id))
    endpoint = endpoint_from_reply({"target": target})
    cwd = (target.get("endpoint") or {}).get("cwd") or (target.get("endpoint") or {}).get("root")
    return ExecutionTarget(
        mode="execution",
        alias=str(target.get("container_name") or endpoint.host),
        endpoint=endpoint,
        execution_id=str(execution_id),
        task_id=task_id_of(client),
        python=target.get("python"),
        cwd=cwd,
        session_id=task_id_of(client),
        service_port=target.get("service_port"),
        launch_preamble=str(target.get("launch_preamble") or ""),
    )


# ---------------------------------------------------------------------------
# Progress / output
# ---------------------------------------------------------------------------

def emit_progress(phase: str, message: str, **extra: Any) -> None:
    envelope_progress(phase, message, **extra)


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def unique_collection_run_dir(
    *,
    tag: str,
    session_id: str | None = None,
    machine: str | None = None,
) -> Path:
    """Allocate ``<state_root>/ascend-profiling-collection/runs/<tag>_<target>``.

    ``safe_run_token`` / ``allocate_run_dir`` come from the plugin domain-lib;
    this wrapper only adds the collection-specific tag+target naming.
    """
    target_token = safe_run_token(session_id or machine or "target")
    tag_token = safe_run_token(tag)
    return allocate_run_dir(
        _collection_state_dir(),
        token=f"{tag_token}_{target_token}",
    )


# ---------------------------------------------------------------------------
# Ascend env preamble (canonical form lives in the plugin domain target module)
# ---------------------------------------------------------------------------

ASCEND_ENV_PREAMBLE = ascend_env_preamble()


# ---------------------------------------------------------------------------
# Local SSH tunnel for sending workload requests from the local machine
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def open_local_tunnel(ep, remote_port: int):
    """Open an ephemeral ``ssh -L`` tunnel to ``127.0.0.1:<remote_port>``.

    Yields a dict with ``local_port`` and ``base_url``. Used by the workload
    sender so multimodal payloads (image data URLs) can be assembled locally
    and POSTed without round-tripping through SSH heredocs. Transport lives
    in ``remote-dev`` ``open_local_forward``.
    """
    require_transport()
    try:
        with open_local_forward(ep, remote_port) as fwd:
            yield {
                "local_port": int(fwd.local_port),
                "base_url": f"http://{fwd.local_host}:{fwd.local_port}",
            }
    except RemoteExecutionError as exc:
        raise RuntimeError(str(exc)) from exc


# ---------------------------------------------------------------------------
# JSON-emitting subprocess wrapper for sibling scripts
# ---------------------------------------------------------------------------

def call_json_command(cmd: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
    """Run ``cmd`` and parse its stdout as JSON.

    Stderr is always relayed so the agent sees progress markers from the
    underlying serving scripts. Raises RuntimeError on non-zero exit
    or non-JSON output, with both streams attached.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd or Path.cwd()),
    )
    stderr_lines: list[str] = []

    def relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()

    thread = threading.Thread(target=relay_stderr, daemon=True)
    thread.start()
    assert proc.stdout is not None
    stdout = proc.stdout.read()
    returncode = proc.wait()
    thread.join(timeout=1)
    stderr = "".join(stderr_lines)
    if returncode != 0:
        raise RuntimeError(
            f"command failed (rc={returncode}): {' '.join(cmd)}\n"
            f"stdout={stdout[:2000]}\nstderr={stderr[:2000]}"
        )
    if not stdout.strip():
        raise RuntimeError(
            f"command produced no output: {' '.join(cmd)}\n"
            f"stderr={stderr[:2000]}"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"command returned non-JSON output: {' '.join(cmd)}\n"
            f"stdout={stdout[:2000]}"
        ) from exc


# ---------------------------------------------------------------------------
# Convenience wrappers around the serving skill's CLI scripts
# ---------------------------------------------------------------------------

def call_serve_start(extra_args: list[str]) -> dict[str, Any]:
    cmd = [sys.executable, str(SERVING_SCRIPTS / "serving.py"), "start", *extra_args]
    return call_json_command(cmd)


def call_serve_stop(
    *,
    context_file: str | None = None,
    execution_id: str | None = None,
    service: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    cmd = [sys.executable, str(SERVING_SCRIPTS / "serving.py"), "stop"]
    if context_file:
        cmd.extend(["--context-file", context_file])
    if execution_id:
        cmd.extend(["--execution-id", execution_id])
    if service:
        cmd.extend(["--service", service])
    if force:
        cmd.append("--force")
    return call_json_command(cmd)
