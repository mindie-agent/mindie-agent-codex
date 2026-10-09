"""Portable process and pinned-source fixtures shared by adapter tests."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


_SCANNER_CACHE = tempfile.TemporaryDirectory(prefix="mindie-scanner-tests-")
_SCANNER = None


def installed_scanner():
    global _SCANNER
    if _SCANNER is None:
        from mindie_knowledge.loop.transcript_redaction import install_scanner
        _SCANNER = install_scanner(Path(_SCANNER_CACHE.name))
    return _SCANNER


def public_engine_config(root, domain="test", **extra):
    """Current production configuration shape for isolated test services."""
    return dict(root=str(root), domain=domain, capture_mode="public-transcript",
                redactor_executable=installed_scanner(),
                transcript_adapter=str(SCRIPTS / "codex_transcript.py"), **extra)


def stop_owned_knowledge_service(engine_config, *, timeout=8):
    """Request shutdown through the exact test engine endpoint and wait for it.

    The shared runtime owns the service process. This fixture owns the whole
    isolated engine, so it requests explicit cancellation, including queued
    work. An updater's stop-if-idle request deliberately refuses that work.
    """
    from urllib.parse import urlparse
    from mindie_knowledge.loop.cli import config_at, connect, rpc
    from mindie_knowledge.loop.locks import lock_held
    from mindie_knowledge.state_layout import state_root

    engine_config = Path(engine_config)
    # A cold Stop returns before its detached starter publishes the service.
    # Wait for that exact test's starter to finish before asking the endpoint
    # to stop; an absent endpoint while startup is pending is not cleanup.
    config = config_at(engine_config)
    domain = Path(config["root"]) / config["domain"]
    wake_path = state_root(config["root"], config["domain"]) / "wake.json"
    try:
        wake_pid = json.loads(wake_path.read_text(encoding="utf-8")).get("wake_pid")
    except FileNotFoundError:
        wake_pid = None
    deadline = time.monotonic() + timeout
    while type(wake_pid) is int and _process_running(wake_pid):
        if time.monotonic() >= deadline:
            raise RuntimeError("owned test startup has not completed")
        time.sleep(.05)
    consumer = domain / "consumer.lock"
    if not consumer.exists() or lock_held(consumer) is False:
        return
    connection = connect(config)
    if urlparse(connection["url"]).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("owned test service endpoint is not loopback")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("owned test startup exhausted the cleanup watchdog")
    receipt = rpc(connection, "stop", timeout=remaining)
    if receipt != {"status": "stopping"}:
        raise RuntimeError("owned test service returned an invalid stop acknowledgement")
    while time.monotonic() < deadline:
        # The service owns this lock until listener and Store.close complete;
        # the RPC acknowledgement alone does not permit deleting SQLite files.
        if lock_held(consumer) is False:
            return
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    raise RuntimeError("owned test service exit unconfirmed; cleanup is not complete")


def _process_running(pid):
    """Read liveness only; a receipt PID never grants permission to kill."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def cleanup_temporary_directory(temporary, *, timeout=5):
    """Close owned diagnostic writers, then wait for acknowledged shutdown handles."""
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


def copy_runtime_scripts(destination):
    """Use a real interpreter with selected fixture helpers as one generation."""
    shutil.copytree(SCRIPTS, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return Path(destination)
