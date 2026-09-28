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
    if result.returncode != 0:
        raise RuntimeError("owned test service did not stop: " + (result.stdout + result.stderr).strip()[:500])


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
