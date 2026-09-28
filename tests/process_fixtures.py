"""Portable process and pinned-source fixtures shared by adapter tests."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


def stop_owned_knowledge_service(engine_config, *, timeout=8):
    """Request shutdown through the exact test engine endpoint and wait for it.

    The shared runtime owns the service process. Tests ask its authenticated
    adapter handoff to stop that engine instead of scanning or killing other
    processes by command text.
    """
    engine_config = Path(engine_config)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "service_handoff.py"), "stop", str(engine_config)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    idle_proven = result.returncode == 0
    if result.returncode != 0 and os.name == "nt":
        # The pinned service may acknowledge an idle stop and close its
        # endpoint while a background thread keeps the process alive. Its
        # CLI reports only an exception class on failed readback, so repeat
        # the exact idle acknowledgement before considering PID cleanup.
        try:
            idle_proven = _request_exact_idle_stop(engine_config)
        except Exception:
            idle_proven = False
    if not idle_proven:
        raise RuntimeError(
            "owned test service did not stop: "
            + (result.stdout + result.stderr).strip()[:500]
        )
    if os.name == "nt":
        _finish_stopped_windows_service(engine_config, timeout=timeout,
                                        idle_proven=idle_proven)


def cleanup_temporary_directory(temporary, *, timeout=5):
    """Wait briefly for Windows antivirus/indexer handles, then clean or fail."""
    _close_test_diagnostic_writers(temporary)
    if os.name != "nt":
        temporary.cleanup()
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            temporary.cleanup()
            return
        except PermissionError:
            _close_test_diagnostic_writers(temporary)
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def _close_test_diagnostic_writers(temporary):
    """Close only pinned diagnostics writers rooted in this test's temp tree.

    The diagnostics package keeps one segment stream open per process and has
    no public scoped shutdown. Its persistent writer otherwise prevents
    Windows from deleting this test-owned directory.
    """
    try:
        from mindie_diagnostics import logging as diagnostic_logging
    except ImportError:
        return
    base = Path(temporary.name).resolve()
    registry = diagnostic_logging._FAILURE_RECORDERS
    lock = diagnostic_logging._LOCK
    with lock:
        for key, recorder in list(registry.items()):
            try:
                root = Path(key[1]).resolve()
                if root != base and base not in root.parents:
                    continue
                recorder.close()
                registry.pop(key, None)
            except (OSError, ValueError, IndexError, TypeError):
                continue


def _request_exact_idle_stop(engine_config):
    """Get a positive stop_if_idle acknowledgement for this exact endpoint."""
    from urllib.parse import urlparse
    from mindie_knowledge.loop.cli import config_at, connect, rpc

    config = config_at(engine_config)
    try:
        connection = connect(config)
    except FileNotFoundError:
        return False
    if urlparse(connection.get("url", "")).hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise RuntimeError("refusing to stop a non-loopback test service")
    result = rpc(connection, "stop_if_idle", timeout=1)
    return isinstance(result, dict) and result.get("idle") is True


def _finish_stopped_windows_service(engine_config, *, timeout, idle_proven):
    """Drain a stopped test service process after endpoint shutdown.

    The pinned service can close its loopback endpoint before its detached
    worker process releases the Windows consumer-lock handle. The lock's PID
    is diagnostic only, so terminate it only after querying that PID and
    proving its command line names this exact temporary engine config.
    """
    engine_config = Path(engine_config).resolve()
    allowed_roots = [
        value for value in (
            os.environ.get("TEMP"), os.environ.get("TMP"),
            os.environ.get("TMPDIR"), os.environ.get("MINDIE_TEST_ROOT"),
        ) if value
    ]
    if not any(
        engine_config == Path(root).resolve()
        or Path(root).resolve() in engine_config.parents
        for root in allowed_roots
    ):
        raise RuntimeError("refusing Windows teardown outside a test-owned temp root")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    taskkill = (
        str(Path(system_root) / "System32" / "taskkill.exe")
        if system_root
        else shutil.which("taskkill.exe") or shutil.which("taskkill")
    )
    if not powershell or not taskkill:
        raise RuntimeError("cannot verify/stop the owned Windows test service")
    script = (
        "$ErrorActionPreference='Stop'; "
        "@(Get-CimInstance Win32_Process | Where-Object { "
        "$_.CommandLine -like '*mindie_knowledge.loop.cli serve*' } | "
        "Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress"
    )
    queried = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=min(5, timeout), check=False,
    )
    expected_config = str(engine_config).casefold()
    if queried.returncode != 0:
        raise RuntimeError("cannot inspect Windows test service processes")
    try:
        processes = json.loads(queried.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError("cannot parse Windows test service process list") from exc
    if isinstance(processes, dict):
        processes = [processes]
    owned = []
    for process in processes:
        command_line = str(process.get("CommandLine") or "").replace('"', "").casefold()
        if (
            "mindie_knowledge.loop.cli" in command_line
            and " serve " in f" {command_line} "
            and expected_config in command_line
        ):
            try:
                process_id = int(process["ProcessId"])
            except (KeyError, TypeError, ValueError):
                continue
            if process_id != os.getpid():
                owned.append(process_id)
    if not owned:
        return
    # Only this temporary engine's exact service processes match. Prefer the
    # acknowledged stop above; if its endpoint is stuck, the test is over and
    # these isolated fixture processes still must not escape teardown.
    for pid in owned:
        subprocess.run(
            [taskkill, "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=min(5, timeout), check=False,
        )
    deadline = time.monotonic() + min(3, timeout)
    while time.monotonic() < deadline:
        check = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=min(5, max(0.2, deadline - time.monotonic())), check=False,
        )
        remaining = json.loads(check.stdout or "[]") if check.returncode == 0 else None
        if isinstance(remaining, dict):
            remaining = [remaining]
        if check.returncode == 0 and not any(
            expected_config in str(item.get("CommandLine") or "").casefold()
            for item in remaining or []
        ):
            return
        time.sleep(0.05)
    raise RuntimeError("owned Windows test service process did not exit")


def extract_git_archive(repository, revision, destination, *members):
    """Extract an exact committed fixture without requiring a `tar` binary."""
    repository = Path(repository)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", revision, *members],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        base = destination.resolve()
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != base and base not in target.parents:
                raise ValueError("fixture archive contains an unsafe path")
        bundle.extractall(destination)
