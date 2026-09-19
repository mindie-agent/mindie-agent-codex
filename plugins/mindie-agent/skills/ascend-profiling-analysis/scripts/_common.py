#!/usr/bin/env python3
"""Shared utilities for ascend-profiling-analysis scripts.

Responsibilities kept minimal on purpose:
  - resolve a coordinator execution or an explicit host/port endpoint
  - run remote bash commands and stream stdout/stderr back
  - tar-sync the framework subtree (``scripts/ascend_profile/``) to the
    remote work dir
  - read / validate the collection skill's manifest
  - manage local run directories under ``.mindie/profiling-analysis/runs/``
  - emit bounded progress receipts on stderr

This script intentionally does NOT contain any profiling analysis logic. The
real pipeline lives next to it under ``scripts/ascend_profile/`` and is run
remotely.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import shlex
import subprocess
import sys

from pathlib import Path
from typing import Any, Iterable

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
import tarfile
import tempfile
import uuid
import time

import mindie_exec as _mindie_exec  # noqa: E402
from mindie_state import allocate_run_dir, state_root  # noqa: E402
from mindie_receipt import PROGRESS_SENTINEL, progress as envelope_progress  # noqa: E402
from mindie_target import (  # noqa: E402
    SshEndpoint,
    print_json as _lib_print_json,
)
from mindie_state import SessionStateError  # noqa: E402
from mindie_jobs import task_client, task_id_of  # noqa: E402

ANALYSIS_STATE_DIR = state_root() / "profiling-analysis" / "runs"

DEFAULT_REMOTE_WORK_DIR = "/tmp/ascend_profile_framework"
SSH_CONNECT_TIMEOUT_SECONDS = 15
DEFAULT_ARTIFACT_PULL_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_JOB_POLL_INTERVAL_S = 2.0
JOB_DONE = frozenset({"succeeded", "completed"})
JOB_FAILED = frozenset({"failed", "error", "timeout", "timed_out", "cancelled", "absent", "lost"})
LAST_REMOTE_JOB_ID: str | None = None
# The analysis framework lives next to this file as a sibling package; it is
# tar-synced to the remote work dir's ``ascend_profile/`` subpath and invoked
# as ``python3 -m ascend_profile.<stage>`` from that work dir.
FRAMEWORK_LOCAL_DIR = Path(__file__).resolve().parent / "ascend_profile"
FRAMEWORK_REMOTE_SUBPATH = "ascend_profile"
FRAMEWORK_PYTHON_MODULE = "ascend_profile"

REQUIRED_SINGLE_ARTIFACTS = (
    "manifest.json",
    "segment_manifest.json",
    "diagnosis_findings.json",
    "report/report.md",
    "report/report.xlsx",
    "report/report.html",
)

# Fast mode (profile_analyze --mode fast) runs the remote analyze with
# --skip-xlsx --skip-host-trace --report-mode summary: report.xlsx is never
# written (the HTML stub still is), and analysis_summary.json becomes the
# primary machine-readable output, so it is required instead.
REQUIRED_SINGLE_ARTIFACTS_FAST = (
    "manifest.json",
    "segment_manifest.json",
    "diagnosis_findings.json",
    "report/report.md",
    "report/analysis_summary.json",
    "report/report.html",
)

# Stage-aware artifact validation: the minimum set of files that must exist
# in the remote output dir once a given stage has finished. Used by the
# wrapper so that ``--only-stage normalize`` doesn't get rejected for not
# producing ``report/report.md``.
#
# The keys match ``ascend_profile.analyze.STAGE_ORDER``; each value is the
# *cumulative* set assumed to be present after that stage runs (so checking
# the end-stage set is enough).
REQUIRED_ARTIFACTS_BY_END_STAGE = {
    "normalize": (
        "manifest.json",
        "normalize_manifest.json",
        "normalized_event_index.csv",
    ),
    "segment": (
        "manifest.json",
        "normalize_manifest.json",
        "segment_manifest.json",
        "step_segments.json",
        "layer_segments.json",
    ),
    "classify": (
        "manifest.json",
        "segment_manifest.json",
        "classify_manifest.json",
        "block_segments.json",
        "class_signatures.json",
    ),
    "summarize": (
        "manifest.json",
        "classify_manifest.json",
        "summary_manifest.json",
        "rank_summary.csv",
        "step_summary.csv",
    ),
    "cross_rank": (
        "manifest.json",
        "summary_manifest.json",
        "cross_rank_manifest.json",
        "cross_rank_alignment.csv",
    ),
    "diagnostics": (
        "manifest.json",
        "summary_manifest.json",
        "diagnosis_findings.json",
    ),
    "report": REQUIRED_SINGLE_ARTIFACTS,
}

# Artifacts that are cheap to pull back to the user's workstation. Big ones
# (normalized_event_index.csv, evidence/bubble_windows.jsonl) are intentionally
# excluded -- agents that need them should ssh in and grep, not download.
LIGHTWEIGHT_PULL_PATHS = (
    "manifest.json",
    "normalize_manifest.json",
    "segment_manifest.json",
    "classify_manifest.json",
    "summary_manifest.json",
    "cross_rank_manifest.json",
    "diagnosis_findings.json",
    "rank_summary.csv",
    "step_summary.csv",
    "step_anatomy.csv",
    "step_class_summary.csv",
    "layer_class_summary.csv",
    "block_class_summary.csv",
    "operator_class_summary.csv",
    "operator_efficiency_summary.csv",
    "model_insights.json",
    "model_context_summary.csv",
    "model_inferred_config.csv",
    "model_feature_summary.csv",
    "model_layer_type_summary.csv",
    "model_candidate_summary.csv",
    "model_config_overview.csv",
    "model_parameter_estimate.csv",
    "model_kv_cache_estimate.csv",
    "model_config_feature_summary.csv",
    "hardware_insights.json",
    "hardware_summary.csv",
    "hardware_theoretical_peaks.csv",
    "hccl_op_summary.csv",
    "hccl_class_summary.csv",
    "wait_anchor_ops.csv",
    "aicpu_summary.csv",
    "report/manifest.json",
    "report/report.md",
    "report/report.xlsx",
    "report/report.html",
    "report/analysis_summary.json",
    # html_report_v2's lazy-loaded data; without it the pulled report.html
    # is a dead shell outside the remote host.
    "report/assets",
    # Per-row giants (block_summary.csv, layer_summary.csv, operator_summary.csv,
    # evidence_index.csv, cross_rank_alignment.*, *_segments.json,
    # class_signatures.json, structure_evidence_graph.json, raw_kernel_index.csv)
    # stay on the remote; use --keep-remote-output to mirror everything.
)

# Fast-mode pull list (profile_analyze --mode fast): only the agent-facing
# compact artifacts come back -- report.md + analysis_summary.json, every
# *_manifest.json, diagnosis_findings.json, and the class-level summary CSVs.
# The bulky per-row tables (evidence_index.csv, cross_rank_alignment.*,
# operator_summary.csv, step_anatomy.csv, layer/block_summary.csv, ...) stay
# on the remote; agents that need them should ssh in and grep.
FAST_PULL_PATHS = (
    "manifest.json",
    "normalize_manifest.json",
    "segment_manifest.json",
    "classify_manifest.json",
    "summary_manifest.json",
    "cross_rank_manifest.json",
    "diagnosis_findings.json",
    "rank_summary.csv",
    "step_summary.csv",
    "step_class_summary.csv",
    "layer_class_summary.csv",
    "block_class_summary.csv",
    "operator_class_summary.csv",
    "hccl_class_summary.csv",
    "report/manifest.json",
    "report/report.md",
    "report/analysis_summary.json",
)


# ---------------------------------------------------------------------------
# SSH endpoint (SshEndpoint itself is imported from mindie_target)
# ---------------------------------------------------------------------------


def get_machine_alias(machine: dict[str, Any]) -> str:
    host = machine.get("host", {})
    if isinstance(host, dict):
        host_ip = host.get("ip", "unknown")
    else:
        host_ip = host or "unknown"
    return machine.get("alias", host_ip)


def resolve_execution_target(
    *,
    context_file: str | None = None,
    execution_id: str | None = None,
    host: str | None = None,
    port: int | None = None,
    user: str = "root",
    service: str | None = None,
) -> dict[str, Any]:
    """Ordinary endpoint for remote analysis I/O. No cwd session resolver."""
    from mindie_target import SshEndpoint, ssh_endpoint_from_mapping
    from mindie_jobs import task_client, task_id_of

    if host:
        endpoint = SshEndpoint(host=host, port=int(port or 22), user=user)
        return {
            "mode": "endpoint",
            "alias": host,
            "endpoint": endpoint,
            "task_id": None,
            "execution_id": None,
            "python": None,
        }
    client = task_client(context_file)
    task_id = task_id_of(client)
    if not execution_id:
        if not service:
            raise SessionStateError("analysis needs --execution-id, --service, or --host")
        execution_id = client.resolve_execution(service=service)
        if not execution_id:
            raise SessionStateError(f"coordinator has no execution named {service!r}")
    observation = client.observe(str(execution_id), "status")
    target = observation.get("target") or {}
    endpoint = ssh_endpoint_from_mapping(target.get("endpoint") or observation.get("endpoint"))
    return {
        "mode": "execution",
        "alias": str(target.get("container_name") or endpoint.host),
        "endpoint": endpoint,
        "task_id": task_id,
        "execution_id": execution_id,
        "python": target.get("python"),
    }


# ---------------------------------------------------------------------------
# Progress / output (thin wrappers over the envelope-owned sentinel)
# ---------------------------------------------------------------------------

def progress(phase: str, message: str, **extra: Any) -> None:
    envelope_progress(phase, message, **extra)


def print_json(data: dict[str, Any]) -> None:
    _lib_print_json(data)


# ---------------------------------------------------------------------------
# Remote command execution through mindie_exec (no copied SSH layer).
#
# The shared domain runtime owns admission, request budgets and remote-dev I/O.
# Short commands return subprocess.CompletedProcess[str]; file content travels
# exclusively through hash-verified artifact transfer.
# ---------------------------------------------------------------------------

def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    timeout: float = 180,
    connect_timeout: float = SSH_CONNECT_TIMEOUT_SECONDS,
):
    return _mindie_exec.ssh_exec(
        endpoint, script, check=check, timeout=timeout,
        connect_timeout=connect_timeout,
    )


def run_remote_job(
    endpoint: SshEndpoint,
    command: str,
    *,
    timeout: int | None,
    name: str,
) -> tuple[int, str]:
    """Launch one remote job and wait until it finishes or the total timeout.

    Never relaunches. Polling is bounded by ``timeout`` seconds.
    """
    if timeout is None or int(timeout) <= 0:
        raise RuntimeError("remote job requires a positive total timeout")
    start_job = getattr(_mindie_exec, "start_job", None)
    job_status = getattr(_mindie_exec, "job_status", None)
    job_stop = getattr(_mindie_exec, "job_stop", None)
    job_tail = getattr(_mindie_exec, "job_tail", None)
    if start_job is None or job_status is None:
        raise RuntimeError(
            "mindie_exec.start_job and mindie_exec.job_status are required "
            "for remote analysis; root must wire remote-dev job tools"
        )
    deadline = time.monotonic() + int(timeout)
    try:
        job_id = start_job(endpoint, command, name=name, timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f"remote job launch failed: {exc}") from exc
    if not job_id:
        raise RuntimeError("remote job did not return an execution reference")
    job_id = str(job_id)
    progress("remote_job", "started", job_id=job_id, timeout_s=int(timeout), name=name)
    global LAST_REMOTE_JOB_ID
    LAST_REMOTE_JOB_ID = job_id
    last_status: dict[str, Any] = {}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if job_stop is not None:
                try:
                    job_stop(endpoint, job_id, force=True, timeout=10)
                except Exception:
                    pass
            raise TimeoutError(
                f"remote job {job_id} exceeded {timeout}s total limit"
            )
        try:
            last_status = dict(job_status(endpoint, job_id, timeout=min(15, remaining)) or {})
        except Exception as exc:
            raise RuntimeError(f"remote job {job_id} status failed: {exc}") from exc
        outcome = str(
            last_status.get("state")
            or last_status.get("status")
            or ""
        ).lower()
        if outcome in JOB_DONE:
            return int(last_status.get("exit_code") or last_status.get("returncode") or 0), job_id
        if outcome in JOB_FAILED:
            tail = ""
            if job_tail is not None:
                try:
                    tail = str(job_tail(endpoint, job_id, lines=80) or "")[:2000]
                except Exception:
                    tail = ""
            raise RuntimeError(
                f"remote job {job_id} {outcome}"
                + (f": {tail}" if tail else "")
            )
        time.sleep(min(DEFAULT_JOB_POLL_INTERVAL_S, max(0.2, remaining)))


def ssh_stream(
    endpoint: SshEndpoint,
    script: str,
    *,
    forward_prefix: str = "[remote] ",
    timeout: int | None = None,
) -> int:
    """Long remote command via the job reference, not a second SSH stream."""
    del forward_prefix
    rc, job_id = run_remote_job(endpoint, script, timeout=timeout, name="ascend-profile")
    progress("remote_job", "stream finished", job_id=job_id, rc=rc)
    return rc


# ---------------------------------------------------------------------------
# Directory sync helpers (rsync is not always installed in Ascend containers).
# Local packing/unpacking uses stdlib tarfile so this module never spawns
# ``tar`` or ``ssh``. Remote unpack/pack still uses the remote ``tar`` binary
# through bounded commands; bytes move through verified artifacts.
# ---------------------------------------------------------------------------

def _tar_name_excluded(name: str, patterns: tuple[str, ...]) -> bool:
    normalized = name.replace("\\", "/").lstrip("./")
    if not normalized or normalized == ".":
        return False
    candidates = (normalized, Path(normalized).name, *Path(normalized).parts)
    return any(
        fnmatch.fnmatch(candidate, pattern)
        for candidate in candidates
        for pattern in patterns
    )


def _tar_bytes_from_directory(local_path: Path, extra_excludes: Iterable[str]) -> bytes:
    patterns = tuple(extra_excludes)
    buf = io.BytesIO()

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if _tar_name_excluded(info.name, patterns):
            return None
        return info

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(local_path), arcname=".", filter=_filter)
    return buf.getvalue()


def sync_to_remote(
    endpoint: SshEndpoint,
    local_path: Path,
    remote_path: str,
    *,
    extra_excludes: Iterable[str] = ("__pycache__", "*.pyc"),
) -> None:
    """Upload an archive through verified artifact transfer to an empty run dir."""
    if not local_path.is_dir():
        raise NotADirectoryError(f"sync source must be a directory: {local_path}")
    progress("parity", "upload analysis code", src=str(local_path), dst=remote_path)
    with tempfile.TemporaryDirectory(prefix="mindie-profile-upload-") as directory:
        archive = Path(directory) / "code.tgz"
        archive.write_bytes(_tar_bytes_from_directory(local_path, extra_excludes))
        remote_tar = f"/tmp/mindie-profile-{uuid.uuid4().hex}.tgz"
        _mindie_exec.artifact_push(endpoint, str(archive), remote_tar)
        quoted = shlex.quote(remote_path)
        # Never erase an existing user's directory to make a sync succeed.
        ssh_exec(endpoint,
            f"trap 'rm -f {remote_tar}' EXIT; mkdir -p {quoted} && "
            f"test -z \"$(find {quoted} -mindepth 1 -maxdepth 1 -print -quit)\" && "
            f"tar -xzf {remote_tar} -C {quoted}", timeout=120)


def sync_from_remote(
    endpoint: SshEndpoint,
    remote_path: str,
    local_path: Path,
    *,
    include_paths: Iterable[str] | None = None,
) -> None:
    """Pull a bounded, hash-verified archive without binary terminal output."""
    local_path.mkdir(parents=True, exist_ok=True)
    progress("artifact_pull", "download analysis artifacts", src=remote_path, dst=str(local_path))
    remote_tar = f"/tmp/mindie-profile-{uuid.uuid4().hex}.tgz"
    if include_paths is None:
        selection = f"tar -czf {remote_tar} ."
    else:
        names = tuple(include_paths)
        if any(Path(p).is_absolute() or ".." in Path(p).parts for p in names):
            raise ValueError("artifact selections must be relative paths within the run")
        args = " ".join(shlex.quote(p) for p in names)
        selection = (
            f"present=(); for p in {args}; do "
            'if [ -e "$p" ]; then present+=("$p"); fi; done; '
            f'tar -czf {remote_tar} --files-from /dev/null "${{present[@]}}"'
        )
    ssh_exec(endpoint, f"cd {shlex.quote(remote_path)} && {selection}", timeout=120)
    try:
        with tempfile.TemporaryDirectory(prefix="mindie-profile-download-") as directory:
            _mindie_exec.artifact_pull(endpoint, remote_tar, directory)
            archive = Path(directory) / Path(remote_tar).name
            if not archive.is_file():
                raise RuntimeError("verified archive was not materialized")
            with tarfile.open(archive, mode="r:gz") as tf:
                tf.extractall(local_path, filter="data")
    finally:
        ssh_exec(endpoint, f"rm -f {remote_tar}", check=False, timeout=15)


# ---------------------------------------------------------------------------
# Run dir / manifest helpers
# ---------------------------------------------------------------------------

def ensure_run_dir(
    tag: str = "",
    *,
    explicit_dir: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Return the local run directory to write pulled artifacts into.

    - When ``explicit_dir`` is given, it is used verbatim.  If the path
      already exists and is non-empty, ``FileExistsError`` is raised unless
      ``overwrite=True``.
    - Otherwise a fresh ``<state-dir>/<utc-timestamp>_<tag>/`` directory is
      allocated (collision-safe) under ``.mindie/profiling-analysis/runs/``.
    """
    if explicit_dir:
        d = Path(explicit_dir).expanduser().resolve()
        if d.exists():
            if d.is_file():
                raise FileExistsError(
                    f"--local-output-dir points at an existing file: {d}"
                )
            if any(d.iterdir()) and not overwrite:
                raise FileExistsError(
                    f"--local-output-dir is not empty: {d}; "
                    "pass --overwrite to use it anyway"
                )
        d.mkdir(parents=True, exist_ok=True)
        return d

    return allocate_run_dir(ANALYSIS_STATE_DIR, tag)


def load_collection_manifest(manifest_path: Path) -> dict[str, Any]:
    """Read and shallow-validate a manifest produced by ascend-profiling-collection.

    The manifest contract is: {schema_version, analysis_status, remote_profile_root, ...}.
    We require ``analysis_status == "ok"`` and ``remote_profile_root`` to be a
    non-empty string. Anything else is a hard fail; this skill never tries to
    repair an incomplete collection.
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"manifest is not valid JSON: {manifest_path} ({e})") from e

    if isinstance(data, dict) and data.get("schema_version") == "mindie.profile-collection.receipt.v1":
        reference = data.get("manifest_ref")
        if not isinstance(reference, str) or not reference:
            raise RuntimeError("collection receipt has no manifest_ref")
        target = Path(reference).expanduser()
        if not target.is_absolute():
            target = manifest_path.parent / target
        # Follow a single recorded artifact, never a chain or a new collection.
        data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("collection manifest must be a JSON object")
    status = data.get("analysis_status")
    if status != "ok":
        raise RuntimeError(
            "collection manifest is not analyzable: "
            f"analysis_status={status!r} at {manifest_path}; "
            "fix the collection run before invoking analysis"
        )

    remote_root = data.get("remote_profile_root")
    if not isinstance(remote_root, str) or not remote_root.strip():
        raise RuntimeError(
            f"manifest missing remote_profile_root: {manifest_path}"
        )
    return data


def remote_python_with_module(
    endpoint: SshEndpoint,
    module: str,
    *,
    required: bool = False,
    python: str | None = None,
) -> str:
    """Use the coordinator-selected interpreter, or python3 on a direct host."""
    candidates = [python] if python else ["python3"]
    for cand in candidates:
        try:
            check = ssh_exec(
                endpoint,
                f"{cand} -c 'import {module}' 2>/dev/null && echo OK || true",
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            if required:
                raise RuntimeError(
                    f"remote python probe timed out after {exc.timeout}s while "
                    f"checking required module {module!r} with candidate {cand!r}; "
                    "check SSH connectivity before starting analysis"
                ) from exc
            continue
        if "OK" in check.stdout:
            return cand
    if required:
        raise RuntimeError(
            f"no supported remote Python can import required module {module!r}; "
            "prepare the runtime with the profiling-analysis requirements "
            "before starting analysis"
        )
    # Optional callers retain the historical fallback to plain python3.
    return "python3"


def quote_remote(path: str) -> str:
    return shlex.quote(path)


# ---------------------------------------------------------------------------
# Wrapper orchestration (shared by profile_analyze.py / profile_sweep.py)
#
# Failure contract: every failure prints one {"status": "failed", ...} JSON
# object on stdout and returns a phase-scoped exit code:
#   2 = pre-remote setup (manifest_validation / resolve / dependency_preflight
#       / setup)
#   3 = parity_sync (framework tar-sync)
#   4 = remote execution (remote_analyze / remote_sweep)
#   5 = validation (artifact_validation / summary_pull)
#   6 = artifact_pull
# ---------------------------------------------------------------------------

WRAPPER_PHASE_EXIT_CODES = {
    "manifest_validation": 2,
    "resolve": 2,
    "dependency_preflight": 2,
    "setup": 2,
    "parity_sync": 3,
    "remote_analyze": 4,
    "remote_sweep": 4,
    "artifact_validation": 5,
    "summary_pull": 5,
    "artifact_pull": 6,
}


def fail_return(phase: str, error: Any, **extra: Any) -> int:
    """Print the wrapper failure JSON for ``phase`` and return its exit code.

    Extra fields whose value is None are dropped so each wrapper keeps its
    historical payload shape.
    """
    payload: dict[str, Any] = {"status": "failed", "phase": phase, "error": str(error)}
    payload.update({k: v for k, v in extra.items() if v is not None})
    print_json(payload)
    return WRAPPER_PHASE_EXIT_CODES[phase]


def resolve_wrapper_target(
    *,
    context_file: str | None = None,
    execution_id: str | None = None,
    host: str | None = None,
    port: int | None = None,
    user: str = "root",
    service: str | None = None,
) -> tuple[dict[str, Any] | None, int | None]:
    """Resolve a coordinator execution or explicit host; on failure emit phase=resolve."""
    from mindie_jobs import TaskTargetError

    try:
        return resolve_execution_target(
            context_file=context_file,
            execution_id=execution_id,
            host=host,
            port=port,
            user=user,
            service=service,
        ), None
    except (ValueError, SessionStateError, TaskTargetError, RuntimeError) as exc:
        return None, fail_return("resolve", exc)


def require_remote_python(
    endpoint: SshEndpoint,
    *,
    alias: str,
    python: str | None = None,
    module: str = "yaml",
) -> tuple[str | None, int | None]:
    """Preflight the coordinator-selected interpreter, or python3 on a direct host."""
    try:
        return remote_python_with_module(
            endpoint, module, required=True, python=python
        ), None
    except RuntimeError as exc:
        return None, fail_return("dependency_preflight", exc, machine=alias)


def prepare_run_dir(
    tag: str,
    *,
    explicit_dir: str | None = None,
    overwrite: bool = False,
    alias: str,
    session_id: str | None = None,
) -> tuple[Path | None, int | None]:
    """Create the local run dir (exit 2 on failure) and log the setup line."""
    try:
        run_dir = ensure_run_dir(tag, explicit_dir=explicit_dir, overwrite=overwrite)
    except FileExistsError as exc:
        return None, fail_return("setup", exc, machine=alias, session_id=session_id)
    progress("setup", "local run dir created", path=str(run_dir))
    return run_dir, None


def sync_framework(
    endpoint: SshEndpoint, remote_work_dir: str, remote_output_dir: str
) -> str:
    """Create the remote dirs and tar-sync ``scripts/ascend_profile/``.

    Returns the remote framework dir. Raises RuntimeError / FileNotFoundError;
    callers convert with ``fail_return("parity_sync", ...)`` (exit 3).
    """
    remote_framework_dir = f"{remote_work_dir}/{FRAMEWORK_REMOTE_SUBPATH}"
    ssh_exec(
        endpoint,
        f"mkdir -p {quote_remote(remote_framework_dir)} "
        f"{quote_remote(remote_output_dir)}",
        check=True,
        timeout=60,
    )
    sync_to_remote(endpoint, FRAMEWORK_LOCAL_DIR, remote_framework_dir)
    return remote_framework_dir


def stream_remote_command(
    endpoint: SshEndpoint,
    cmd: str,
    *,
    forward_prefix: str,
    timeout: int | None,
    fail_phase: str,
    **fail_extra: Any,
) -> tuple[int | None, int | None]:
    """Run ``ssh_stream`` with the wall-clock budget.

    Returns ``(rc, None)`` on completion; a wall-clock TimeoutError prints the
    ``fail_phase`` failure JSON (exit 4) and returns ``(None, code)``.
    """
    try:
        rc, job_id = run_remote_job(
            endpoint, cmd, timeout=timeout, name=str(fail_extra.get("job_name") or fail_phase)
        )
        global LAST_REMOTE_JOB_ID
        LAST_REMOTE_JOB_ID = job_id
        progress("remote_job", "finished", job_id=job_id, rc=rc, phase=fail_phase)
        return rc, None
    except TimeoutError as exc:
        return None, fail_return(fail_phase, exc, **fail_extra)
    except RuntimeError as exc:
        return None, fail_return(fail_phase, exc, **fail_extra)


def _remote_bytes(endpoint: SshEndpoint, remote_path: str) -> int:
    listing = ssh_exec(
        endpoint,
        f"du -sb {quote_remote(remote_path)} 2>/dev/null | awk '{{print $1}}'",
        check=False,
        timeout=60,
    )
    text = str(listing.stdout or "").strip().splitlines()
    if listing.returncode or not text:
        raise RuntimeError("could not determine remote artifact size; pull not started")
    try:
        size = int(text[0].split()[0])
    except ValueError as exc:
        raise RuntimeError("invalid remote artifact size; pull not started") from exc
    if size < 0:
        raise RuntimeError("invalid remote artifact size; pull not started")
    return size


def pull_artifacts(
    endpoint: SshEndpoint,
    remote_output_dir: str,
    run_dir: Path,
    *,
    keep_remote_output: bool,
    include_paths: Iterable[str],
    max_bytes: int = DEFAULT_ARTIFACT_PULL_MAX_BYTES,
) -> None:
    """Pull artifacts back to the local run dir.

    Raises RuntimeError; callers convert with ``fail_return("artifact_pull",
    ...)`` (exit 6). Refuses a pull larger than ``max_bytes``.
    """
    size = _remote_bytes(endpoint, remote_output_dir)
    if size > max_bytes:
        raise RuntimeError(
            f"remote output {remote_output_dir} is {size} bytes, over the "
            f"{max_bytes} byte pull cap; use --no-pull or raise the cap"
        )
    if keep_remote_output:
        sync_from_remote(endpoint, remote_output_dir, run_dir)
    else:
        sync_from_remote(
            endpoint, remote_output_dir, run_dir, include_paths=include_paths
        )
