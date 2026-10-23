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
from contextlib import contextmanager


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


_SCANNER_CACHE = tempfile.TemporaryDirectory(prefix="mindie-scanner-tests-")
_SCANNER = None


def native_fixture(path, source):
    """Run the same Python CLI fixture through each platform's executable entry."""
    path = Path(path)
    script = path.with_suffix(".py")
    script.write_text(source, encoding="utf-8", newline="\n")
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        command = f'@echo off\n"{sys.executable}" "{script}" %*\n'
    else:
        import shlex
        command = "#!/bin/sh\nexec " + shlex.join([sys.executable, str(script)]) + ' "$@"\n'
    path.write_text(command, encoding="utf-8", newline="\n")
    path.chmod(0o700)
    return path


def installed_scanner():
    global _SCANNER
    if _SCANNER is None:
        from mindie_knowledge.loop.transcript_redaction import install_scanner
        _SCANNER = install_scanner(Path(_SCANNER_CACHE.name))
    return _SCANNER


def public_engine_config(root, domain="test", *, adapter_profile=None, **extra):
    """Current production configuration shape for isolated test services."""
    result = dict(root=str(root), domain=domain, capture_mode="public-transcript",
                  redactor_executable=installed_scanner(),
                  transcript_adapter=str(SCRIPTS / "codex_transcript.py"), **extra)
    if adapter_profile is not None:
        profile = Path(adapter_profile).resolve()
        result.update(
            service_launch_context=dict(paths={
                "MINDIE_AGENT_CONFIG": str(profile),
                "CODEX_HOME": str(profile.parent / "codex-home"),
            }, environment=[]),
            summary_command=[sys.executable, str(Path(__file__).with_name("no_model_summary.py"))],
            product_validation=dict(schema="mindie-candidate-check/1", publication={},
                                    native_version="0.0.1", source_sha256="a" * 64),
        )
    return result


@contextmanager
def owned_fixture_process(command, **kwargs):
    """The test parent retains the Job/group until all its writers are gone."""
    from mindie_knowledge.loop.process import terminate_tree

    if os.name == "nt":
        from mindie_knowledge.windows_process import spawn_owned
        process = spawn_owned(command, **kwargs)
    else:
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        yield process
    finally:
        primary = sys.exc_info()[1]
        try:
            terminate_tree(process, wait=True)
            process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        except Exception as exc:
            if primary is None:
                raise
            primary.add_note("owned fixture cleanup also failed: " + repr(exc))


def run_owned_fixture(command, *, input="", timeout, diagnose, **kwargs):
    """A test watchdog, with retained output and no unowned timed-out helper."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            with owned_fixture_process(
                command, stdin=subprocess.PIPE, stdout=output, stderr=error, **kwargs
            ) as process:
                try:
                    process.communicate(input.encode("utf-8"), timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    try:
                        exc.add_note("fixture stage at timeout: " + json.dumps(diagnose()))
                    except Exception as diagnostic_error:
                        exc.add_note("fixture diagnosis also failed: " + repr(diagnostic_error))
                    raise
        except subprocess.TimeoutExpired as exc:
            output.seek(0)
            error.seek(0)
            exc.output = output.read().decode("utf-8", errors="replace")
            exc.stderr = error.read().decode("utf-8", errors="replace")
            exc.add_note("helper stdout: " + exc.output)
            exc.add_note("helper stderr: " + exc.stderr)
            raise
        output.seek(0)
        error.seek(0)
        return subprocess.CompletedProcess(
            command, process.returncode,
            output.read().decode("utf-8", errors="replace"),
            error.read().decode("utf-8", errors="replace"),
        )


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
    from mindie_knowledge.filesystem import filesystem_path

    engine_config = Path(engine_config)
    # A cold Stop returns before its detached starter publishes the service.
    # Wait for that exact test's starter to finish before asking the endpoint
    # to stop; an absent endpoint while startup is pending is not cleanup.
    config = config_at(engine_config)
    domain = filesystem_path(config["root"]) / config["domain"]
    if not domain.exists():
        return
    deadline = time.monotonic() + timeout
    # A service can still be creating its layout. Observe its real startup
    # locks before reading layout; a missing/corrupt layout is never ignored.
    for name in ("start.lock", "state-layout.lock"):
        lock = domain / name
        while lock.exists() and lock_held(lock) is not False:
            if time.monotonic() >= deadline:
                raise RuntimeError("owned test startup lock has not been released: " + name)
            time.sleep(.02)
    wake_path = state_root(config["root"], config["domain"]) / "wake.json"
    try:
        wake_pid = json.loads(wake_path.read_text(encoding="utf-8")).get("wake_pid")
    except FileNotFoundError:
        wake_pid = None
    while type(wake_pid) is int and _process_running(wake_pid):
        if time.monotonic() >= deadline:
            raise RuntimeError("owned test startup has not completed")
        time.sleep(.02)
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
        time.sleep(min(.02, max(0, deadline - time.monotonic())))
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
    from mindie_knowledge.filesystem import filesystem_path
    # Core writes extended-length descendants even beneath a short test root.
    # Use that same namespace for removal and retain failures for inspection.
    target = str(filesystem_path(temporary.name))
    temporary._finalizer.detach()
    deadline = time.monotonic() + timeout
    while True:
        try:
            temporary._rmtree(target, ignore_errors=False)
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
            except TimeoutError:
                raise
            except (OSError, ValueError, IndexError, TypeError):
                continue


_ARCHIVE_CACHE_DIR = tempfile.TemporaryDirectory(prefix="mindie-archive-tests-")
_ARCHIVE_CACHE = {}


def extract_git_archive(repository, revision, destination, *members):
    """Extract an exact committed fixture without requiring a `tar` binary."""
    repository = Path(repository)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    key = (str(repository.resolve()), revision, tuple(members))
    cached = _ARCHIVE_CACHE.get(key)
    if cached is None:
        cached = Path(_ARCHIVE_CACHE_DIR.name) / str(len(_ARCHIVE_CACHE))
        cached.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "-C", str(repository), "archive", revision, *members],
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            base = cached.resolve()
            for member in bundle.getmembers():
                target = (cached / member.name).resolve()
                if target != base and base not in target.parents:
                    raise ValueError("fixture archive contains an unsafe path")
            bundle.extractall(cached)
        _ARCHIVE_CACHE[key] = cached
    shutil.copytree(cached, destination, dirs_exist_ok=True)


def copy_runtime_scripts(destination):
    """Use a real interpreter with selected fixture helpers as one generation."""
    shutil.copytree(SCRIPTS, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return Path(destination)


def recorded_wake_runtime(config_path, destination):
    """Keep real admission/capture; record only the external service wake seam.

    Local envelope and queue tests must not depend on an interactive Windows
    Scheduler token or race a background consumer against their explicit drain.
    Native service ownership and readiness have separate integration coverage.
    """
    scripts = copy_runtime_scripts(destination)
    original = scripts / "production_admission_ops.py"
    (scripts / "admission_ops.py").rename(original)
    receipt = scripts / "wake-requests.jsonl"
    (scripts / "admission_ops.py").write_text(
        "import json, runpy\n"
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "from mindie_knowledge.loop import handoff\n"
        "def request_wake(config_path, *, session_id, event=None, timeout=None, **_kwargs):\n"
        "    row = dict(config=str(config_path), session=session_id, event=event)\n"
        f"    with Path({str(receipt)!r}).open('a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(row) + '\\n')\n"
        "    return dict(wake='requested', runtime='unavailable', reason='recorded-test-transport')\n"
        "with patch.object(handoff, 'request_wake', request_wake):\n"
        f"    runpy.run_path({str(original)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runtime_scripts"] = str(scripts)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return receipt


def wake_requests(receipt):
    if not receipt.exists():
        return []
    return [json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()]


def consume_queued_captures(config_path):
    """Run the actual capture engine after a test-controlled wake request."""
    from contextlib import closing
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.cli import config_at, load_transcript_adapter
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.store import Store

    config = config_at(config_path)
    with closing(Store(config["root"], config["domain"])) as store:
        engine = Engine(
            store, capture_mode=config["capture_mode"],
            settings_path=config["community_config"],
            admission=Admission(config["admission_path"]),
            transcript_adapter=load_transcript_adapter(config),
            redactor_executable=config["redactor_executable"],
        )
        rows = store.db.execute(
            "SELECT id FROM captures WHERE status IN ('queued','pending','deferred')"
        ).fetchall()
        for row in rows:
            engine._process(row["id"])
