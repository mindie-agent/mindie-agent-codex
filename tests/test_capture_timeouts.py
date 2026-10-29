"""Filesystem and reader deadlines reach the owner without changing settled work."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import bounded_process
import capture_spool
import native_hooks
import process_guard


def test_retention_timeout_reaches_capture_owner(tmp_path, monkeypatch):
    old = tmp_path / "old.spool"
    old.write_bytes(b"old")
    capture = capture_spool.Capture(tmp_path, 16)
    capture.add("out", b"new")
    failure = TimeoutError("retention filesystem deadline")
    unlink = Path.unlink

    def timed_unlink(path, *args, **kwargs):
        if path == old:
            raise failure
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(capture_spool, "RETENTION_FILES", 0)
    monkeypatch.setattr(Path, "unlink", timed_unlink)
    with pytest.raises(TimeoutError) as caught:
        capture.freeze()
    assert caught.value is failure
    assert old.read_bytes() == b"old"
    assert Path(capture.receipt()["stdout"]["path"]).read_bytes() == b"new"


def test_spool_finalization_timeout_preserves_real_completed_process(tmp_path, monkeypatch):
    capture = capture_spool.Capture(tmp_path / "spool", 256)
    original = os.replace

    def timed_replace(source, destination):
        if str(source).endswith("-stdout.active"):
            raise TimeoutError("spool rename deadline")
        return original(source, destination)

    monkeypatch.setattr(capture_spool.os, "replace", timed_replace)
    result = bounded_process.run(
        [sys.executable, "-c", "print('completed')"], "", capture=capture,
        timeout=5, allowed_returncodes=None,
    )
    assert result.execution == "completed" and result.returncode == 0
    assert result.stdout == "completed\n"
    assert result.cleanup == [{"stage": "finalize_capture", "error_type": "TimeoutError"}]
    assert result.capture["spool_error"] == ["TimeoutError"]
    assert "path" not in result.capture["stdout"]
    assert "sha256" not in result.capture["stdout"]
    assert Path(result.capture["stderr"]["path"]).exists()
    assert all(stream.handle is None for stream in capture.streams.values())


def test_spool_write_timeout_preserves_unknown_execution_and_no_false_artifact(tmp_path):
    capture = capture_spool.Capture(tmp_path / "spool", 256)
    stream = capture.streams["out"]
    handle = stream.handle
    failure = TimeoutError("spool write deadline")

    class TimedWriter:
        def write(self, chunk):
            handle.write(chunk)
            raise failure

        def close(self):
            handle.close()

    stream.handle = TimedWriter()
    with pytest.raises(TimeoutError) as caught:
        bounded_process.run(
            [sys.executable, "-c", "print('output', flush=True)"], "",
            capture=capture, timeout=5,
        )
    assert caught.value is failure
    result = caught.value.process_result
    assert result.execution == "unknown"
    assert result.capture["spool_error"] == ["TimeoutError"]
    assert "path" not in result.capture["stdout"]
    assert handle.closed


class TimedReader:
    def __init__(self, handle, failure):
        self.handle = handle
        self.failure = failure

    def read(self, *args):
        raise self.failure

    def readline(self, *args):
        raise self.failure

    def close(self):
        self.handle.close()

    @property
    def closed(self):
        return self.handle.closed


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX child; exercises threaded reader algorithm")
def test_threaded_reader_timeout_is_raised_by_owner_and_child_is_reaped():
    failure = TimeoutError("reader deadline")
    process = bounded_process._spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"], subprocess.DEVNULL, None,
    )
    original = process.stdout
    process.stdout = TimedReader(original, failure)
    with pytest.raises(TimeoutError) as caught:
        bounded_process._run_windows(process, 5, 1024, None)
    assert caught.value is failure
    assert caught.value.process_result.execution == "unknown"
    assert process.poll() is not None
    assert original.closed and process.stderr.closed


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX process ownership")
@pytest.mark.parametrize("owner", ["native_hooks", "process_guard"])
def test_native_reader_timeout_reaches_main_call_and_cleans_process(monkeypatch, tmp_path, owner):
    failure = TimeoutError("native reader deadline")
    processes = []
    spawn = bounded_process._spawn

    def timed_spawn(command, stdin, env, **kwargs):
        process = spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin, env, **kwargs,
        )
        process.stdout = TimedReader(process.stdout, failure)
        processes.append(process)
        return process

    if owner == "native_hooks":
        monkeypatch.setattr(native_hooks, "_spawn", timed_spawn)
        invoke = lambda: native_hooks.NativeHooks("unused", dict(os.environ, CODEX_HOME=str(tmp_path)))
    else:
        monkeypatch.setattr(bounded_process, "_spawn", timed_spawn)
        invoke = lambda: process_guard.run_codex(["unused"], "", timeout=5)
    with pytest.raises(TimeoutError) as caught:
        invoke()
    assert caught.value is failure
    assert len(processes) == 1
    process = processes[0]
    assert process.poll() is not None
    assert process.stdout.closed and process.stderr.closed
