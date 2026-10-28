"""Component calls spool bounded output, validate before spawn and recover partial receipts."""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import bounded_process
import capture_spool
import mcp_gate

CHILD = Path(__file__).parent / "fixtures/spool_child.py"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, runtime_scripts=str(SCRIPTS),
        component_commands=dict(sim=[sys.executable, str(CHILD)]),
    )))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    monkeypatch.setenv("MINDIE_COORDINATOR_STATE_DIR", str(tmp_path / "coordinator"))
    return mcp_gate.Gate("components")


def call(gate, name, arguments, cancel=None):
    return gate.call(dict(id=1, params=dict(name=name, arguments=arguments, _meta={
        "threadId": "t", "x-codex-turn-metadata": dict(
            thread_id="t", session_id="t", turn_id="turn-1")})), cancel=cancel)


def test_one_mebibyte_file_output_succeeds_with_small_preview(gate, tmp_path):
    marker = tmp_path / "marker"
    result = call(gate, "sim", dict(argv=["file", str(marker)], max_output_tokens=1))
    value = result["structuredContent"]
    assert not result["isError"] and value["exit_code"] == 0
    assert "result" not in value and len(value["stdout"]) <= 4
    receipt = value["capture"]["stdout"]
    assert receipt["bytes"] == 1024 * 1024 and receipt["preview_truncated"]
    assert hashlib.sha256(Path(receipt["path"]).read_bytes()).hexdigest() == receipt["sha256"]
    assert marker.read_text() == "effect"


def test_true_nonzero_exit_stays_an_error_with_stderr(gate, tmp_path):
    result = call(gate, "sim", dict(argv=["fail", str(tmp_path / "m")]))
    assert result["isError"]
    assert result["structuredContent"]["exit_code"] == 3
    assert "real failure" in result["structuredContent"]["stderr"]


def test_timeout_keeps_side_effect_and_partial_receipt(gate, tmp_path):
    marker = tmp_path / "marker"
    result = call(gate, "sim", dict(argv=["hang", str(marker)], timeout_ms=1500))
    assert result["isError"]
    structured = result["structuredContent"]
    assert structured["code"] == "timeout" and structured["automatic_retry"] is False
    assert structured["execution"] != "not_started"
    assert marker.read_text() == "effect"
    assert "partial-start" in structured["partial"]["stdout"]
    assert structured["partial"]["capture"]["stdout"]["bytes"] == len("partial-start")


def test_cancel_keeps_side_effect_and_partial_receipt(gate, tmp_path):
    marker = tmp_path / "marker"
    event = threading.Event()
    timer = threading.Timer(1.5, event.set)
    timer.start()
    try:
        result = call(gate, "sim", dict(argv=["hang", str(marker)]), cancel=event)
    finally:
        timer.cancel()
    structured = result["structuredContent"]
    assert result["isError"] and structured["code"] == "cancelled"
    assert marker.read_text() == "effect"
    assert structured["partial"]["capture"]["stdout"]["bytes"] > 0


def test_hard_cap_kills_child_and_keeps_bounded_spool(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(capture_spool, "HARD_CAP", 256 * 1024)
    marker = tmp_path / "marker"
    result = call(gate, "sim", dict(argv=["flood", str(marker)], max_output_tokens=8))
    structured = result["structuredContent"]
    assert result["isError"] and structured["code"] == "output_limit"
    assert marker.read_text() == "effect"
    capture = structured["partial"]["capture"]
    assert capture["hard_cap_reached"] and capture["stdout"]["bytes"] == 256 * 1024
    assert len(Path(capture["stdout"]["path"]).read_bytes()) == 256 * 1024


@pytest.mark.parametrize("name,arguments,field", [
    ("sim", dict(argv="x"), "argv"),
    ("sim", dict(argv=["a", 1]), "argv"),
    ("sim", dict(argv=["a\0b"]), "argv"),
    ("sim", dict(argv=["a"], timeout_ms=True), "timeout_ms"),
    ("sim", dict(argv=["a"], timeout_ms=1.5), "timeout_ms"),
    ("sim", dict(argv=["a"], timeout_ms=0), "timeout_ms"),
    ("sim", dict(argv=["a"], max_output_tokens=10**9), "max_output_tokens"),
    ("diagnostics_bundle", dict(root=5), "root"),
    ("diagnostics_bundle", dict(root=""), "root"),
    ("diagnostics_bundle", dict(root="r", operation_id=""), "operation_id"),
    ("diagnostics_bundle", dict(root="r", output=["o"]), "output"),
    ("diagnostics_status", dict(config="c\0"), "config"),
])
def test_invalid_arguments_are_rejected_before_spawn(gate, monkeypatch, name, arguments, field):
    def forbidden(*args, **kwargs):
        raise AssertionError("spawned")
    monkeypatch.setattr(bounded_process, "_spawn", forbidden, raising=False)
    result = call(gate, name, arguments)
    structured = result["structuredContent"]
    assert result["isError"] and structured["code"] == "invalid_arguments"
    assert structured["execution"] == "not_started" and structured["field"] == field


def test_generic_errors_never_echo_raw_text(gate):
    canary = "SECRET-CANARY-7731"
    result = mcp_gate.call_failure(RuntimeError(canary))
    assert canary not in json.dumps(result)
    assert result["structuredContent"]["code"] == "internal_error"


def test_strict_runner_keeps_overflow_error():
    with pytest.raises(ValueError):
        bounded_process.run([sys.executable, "-c", "print('x'*5000)"], "", timeout=10, max_output=100)


def _complete(directory, size=3, clock=None):
    capture = capture_spool.Capture(directory, 4)
    capture.add("out", b"x" * size)
    capture.close()
    return capture


def _completed(directory):
    return sorted(Path(directory).glob("*.spool"))


def test_retention_caps_file_count_in_a_fast_burst(tmp_path):
    for _ in range(80):
        _complete(tmp_path)
    assert len(_completed(tmp_path)) <= capture_spool.RETENTION_FILES


def test_retention_caps_bytes_and_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(capture_spool, "RETENTION_BYTES", 100)
    paths = []
    for _ in range(6):
        capture = _complete(tmp_path, size=40)
        paths.append(capture.streams["out"].path)
    kept = [str(path) for path in _completed(tmp_path)]
    assert sum(Path(path).stat().st_size for path in kept) <= 100
    assert paths[-1] in kept and capture.receipt()["stdout"]["path"] == paths[-1]
    assert hashlib.sha256(Path(paths[-1]).read_bytes()).hexdigest() == capture.receipt()["stdout"]["sha256"]


def test_retention_age_uses_controlled_clock(tmp_path):
    import os
    old = tmp_path / "old.spool"
    fresh = tmp_path / "fresh.spool"
    old.write_text("a")
    fresh.write_text("b")
    os.utime(old, (1000, 1000))
    os.utime(fresh, (1000 + 3600, 1000 + 3600))
    now = 1000 + capture_spool.RETENTION_SECONDS + 10
    assert capture_spool.prune(tmp_path, now=now) == []
    assert not old.exists() and fresh.exists()


def test_retention_never_removes_active_capture_and_close_is_idempotent(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(capture_spool, "RETENTION_FILES", 1)
    active = capture_spool.Capture(tmp_path, 4)
    active.add("out", b"live")
    active_path = Path(active.streams["out"].path)
    assert active_path.suffix == ".active"
    os.utime(active_path, (1, 1))
    for _ in range(5):
        _complete(tmp_path)
    assert active_path.exists()
    active.close()
    active.close()
    receipt = active.receipt()["stdout"]
    assert Path(receipt["path"]).read_bytes() == b"live" and not active_path.exists()


def test_concurrent_disappearance_is_not_fatal(tmp_path, monkeypatch):
    for _ in range(3):
        _complete(tmp_path)
    monkeypatch.setattr(capture_spool, "RETENTION_FILES", 0)
    real = Path.unlink

    def vanish(self, *args, **kwargs):
        real(self)
        raise FileNotFoundError(str(self))
    monkeypatch.setattr(Path, "unlink", vanish)
    assert capture_spool.prune(tmp_path) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_spool_directory_is_private(tmp_path):
    capture = _complete(tmp_path / "spool")
    assert (tmp_path / "spool").stat().st_mode & 0o777 == 0o700
    assert Path(capture.receipt()["stdout"]["path"]).stat().st_mode & 0o777 == 0o600


def _fake_result(cleanup, returncode=0):
    result = bounded_process.ProcessResult("completed", returncode=returncode)
    result.cleanup = cleanup
    return result


@pytest.mark.parametrize("cleanup", [
    [dict(stage="close_selector", error_type="OSError")],
    [dict(stage="terminate_owned_tree", error_type="PermissionError"),
     dict(stage="join_reader", error_type="ReaderStillRunning")],
])
def test_cleanup_warning_keeps_completed_success(gate, tmp_path, monkeypatch, cleanup):
    import component_tools
    marker = tmp_path / "side-effect"

    def fake_run(command, data, **kwargs):
        marker.write_text("effect")
        capture = kwargs["capture"]
        capture.add("out", b'{"ok": true}')
        capture.add("err", b"warn")
        result = _fake_result(list(cleanup))
        bounded_process._settle_capture(result, capture)
        return result
    monkeypatch.setattr(component_tools, "run", fake_run)
    result = call(gate, "sim", dict(argv=["x"]))
    value = result["structuredContent"]
    assert not result["isError"] and value["exit_code"] == 0
    assert value["result"] == dict(ok=True) and value["stderr"] == "warn"
    assert value["cleanup_warning"] == cleanup
    assert marker.read_text() == "effect"
    assert Path(value["capture"]["stdout"]["path"]).read_bytes() == b'{"ok": true}'


def test_cleanup_warning_keeps_real_nonzero_exit(gate, monkeypatch):
    import component_tools

    def fake_run(command, data, **kwargs):
        kwargs["capture"].add("err", b"business failure")
        result = _fake_result([dict(stage="close_selector", error_type="OSError")], returncode=5)
        bounded_process._settle_capture(result, kwargs["capture"])
        return result
    monkeypatch.setattr(component_tools, "run", fake_run)
    result = call(gate, "sim", dict(argv=["x"]))
    value = result["structuredContent"]
    assert result["isError"] and value["exit_code"] == 5 and "business failure" in value["stderr"]
    assert value["cleanup_warning"][0]["stage"] == "close_selector"


def test_spool_write_failure_keeps_success_and_claims_no_artifact(gate, tmp_path, monkeypatch):
    original = capture_spool._Stream.write

    def failing(self, chunk):
        if self.handle is not None:
            self.handle.close()
            self.handle = open(os.devnull, "rb")
        return original(self, chunk)
    import os
    monkeypatch.setattr(capture_spool._Stream, "write", failing)
    marker = tmp_path / "marker"
    result = call(gate, "sim", dict(argv=["file", str(marker)], max_output_tokens=1))
    value = result["structuredContent"]
    assert not result["isError"] and value["exit_code"] == 0 and marker.read_text() == "effect"
    receipt = value["capture"]
    assert "spool_error" in receipt
    assert "path" not in receipt["stdout"] and "sha256" not in receipt["stdout"]


def test_late_reader_chunks_cannot_change_a_frozen_capture(tmp_path):
    capture = capture_spool.Capture(tmp_path, 16)
    capture.add("out", b"abc")
    capture.freeze()
    capture.add("out", b"late")
    receipt = capture.receipt()["stdout"]
    assert receipt["bytes"] == 3 and capture.stdout() == "abc"
    assert Path(receipt["path"]).read_bytes() == b"abc"
    assert hashlib.sha256(b"abc").hexdigest() == receipt["sha256"]


def test_windows_failure_freezes_capture_before_settle(tmp_path, monkeypatch):
    import io
    import time

    class Process:
        def __init__(self):
            self.stdout = io.BytesIO(b"partial")
            self.stderr = io.BytesIO(b"")
            self.release = threading.Event()

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    class SlowReader(io.BytesIO):
        def __init__(self, release):
            super().__init__(b"")
            self.release = release
            self.sent = False
            self.late_sent = False

        def read(self, size=-1):
            if not self.sent:
                self.sent = True
                return b"first"
            if self.late_sent:
                return b""
            self.release.wait(5)
            self.late_sent = True
            return b"after-settle"

        def read1(self, size=-1):
            return self.read(size)

    process = Process()
    process.stderr = SlowReader(process.release)
    monkeypatch.setattr(bounded_process, "_kill_tree", lambda proc: None)
    capture = capture_spool.Capture(tmp_path, 64)
    with pytest.raises(TimeoutError) as caught:
        bounded_process._run_windows(process, 0.2, 1024, None, None, capture=capture)
    process.release.set()
    time.sleep(0.2)
    result = caught.value.process_result
    assert result.stderr == "first" and result.stdout == "partial"
    assert result.capture["stderr"]["bytes"] == 5
    path = Path(result.capture["stderr"]["path"])
    assert path.read_bytes() == b"first"
    assert hashlib.sha256(b"first").hexdigest() == result.capture["stderr"]["sha256"]


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX; Windows CI covers run()")
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_windows_reader_keeps_flushed_partial_pipe_output(tmp_path, mode):
    import subprocess
    import time
    ready = tmp_path / "ready"
    code = ("import time,sys,pathlib; print('partial-start', flush=True); "
            "pathlib.Path(sys.argv[1]).write_text('x'); time.sleep(30)")
    process = subprocess.Popen([sys.executable, "-c", code, str(ready)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    cancel = threading.Event()

    def trigger():
        end = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.3)
        cancel.set()

    helper = threading.Thread(target=trigger, daemon=True)
    helper.start()
    capture = capture_spool.Capture(tmp_path / "spool", 1000)
    timeout = 10 if mode == "cancel" else 1.5
    expected = RuntimeError if mode == "cancel" else TimeoutError
    with pytest.raises(expected) as caught:
        bounded_process._run_windows(process, timeout, 1000, cancel if mode == "cancel" else None,
                                     None, capture=capture)
    helper.join(5)
    result = caught.value.process_result
    assert result.stdout == "partial-start\n"
    assert result.capture["stdout"]["bytes"] == len("partial-start\n")
    assert process.poll() is not None


def _validate(schema_tool, arguments):
    import jsonschema
    jsonschema.validate(arguments, schema_tool["inputSchema"])


BOUNDARY_CASES = [
    ("sim", dict(argv=["a"] * 256), True),
    ("sim", dict(argv=["a"] * 257), False),
    ("sim", dict(argv=["a" * 65536]), True),
    ("sim", dict(argv=["a" * 65537]), False),
    ("sim", dict(argv=["a\0b"]), False),
    ("sim", dict(argv=["-x"]), True),
    ("sim", dict(argv=["a"], timeout_ms=300000), True),
    ("sim", dict(argv=["a"], timeout_ms=300001), False),
    ("sim", dict(argv=["a"], max_output_tokens=262144), True),
    ("sim", dict(argv=["a"], max_output_tokens=0), False),
    ("diagnostics_bundle", dict(root="r" * 4096), True),
    ("diagnostics_bundle", dict(root="r" * 4097), False),
    ("diagnostics_bundle", dict(root=""), False),
    ("diagnostics_bundle", dict(root="r\0"), False),
    ("diagnostics_bundle", dict(root="r", operation_id="-leading"), True),
    ("diagnostics_bundle", dict(root="-r", output="-o"), True),
    ("diagnostics_status", dict(config=""), False),
    ("diagnostics_status", dict(config="-c"), True),
]


@pytest.mark.parametrize("name,arguments,valid", BOUNDARY_CASES)
def test_schema_and_runtime_agree_on_boundaries(name, arguments, valid):
    import jsonschema
    import component_tools
    tool = next(item for item in component_tools.catalog() if item["name"] == name)
    try:
        _validate(tool, arguments)
        schema_valid = True
    except jsonschema.ValidationError:
        schema_valid = False
    try:
        component_tools.validate(name, arguments)
        runtime_valid = True
    except mcp_gate.InvalidToolArguments:
        runtime_valid = False
    assert schema_valid == runtime_valid == valid


def test_dash_prefixed_paths_use_attached_option_values():
    import component_tools
    assert component_tools._build("diagnostics_bundle", dict(root="-r", operation_id="-op", output="-o")) == (
        "diagnostics", ["bundle", "--root=-r", "--operation-id=-op", "--output=-o"])
    assert component_tools._build("diagnostics_status", dict(config="-c")) == (
        "diagnostics", ["reporting", "status", "--config=-c"])


def _remote_failure(monkeypatch, tmp_path, exc):
    import runtime_call
    import remote_dev.mcp.tools as tools
    monkeypatch.setenv("MINDIE_REMOTE_STATE_DIR", str(tmp_path / "remote"))
    monkeypatch.setattr(tools, "call_tool", lambda name, args: (_ for _ in ()).throw(exc))
    return runtime_call.remote(dict(remote_session_id="abcdef123456", name="remote_bash",
                                    arguments=dict(command="true")))


def test_remote_caller_error_gets_static_hint_and_public_field(monkeypatch, tmp_path):
    from remote_dev.core.errors import CALLER_ERROR_HINTS, caller_error
    code = next(iter(CALLER_ERROR_HINTS))
    shaped = _remote_failure(monkeypatch, tmp_path, caller_error("RAW-SECRET", error_code=code, field="command"))
    details = shaped["structuredContent"]["error_details"]
    assert details["error_code"] == code and details["field"] == "command"
    assert CALLER_ERROR_HINTS[code] in shaped["content"][0]["text"]
    assert "RAW-SECRET" not in json.dumps(shaped)


def test_real_auth_failure_keeps_safe_recovery_hint(monkeypatch, tmp_path):
    import runtime_call
    from remote_dev.core.errors import AUTH_ERROR_HINTS

    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("PRIVATE_MISSING_SSH_PASSWORD", raising=False)
    shaped = runtime_call.remote(dict(
        remote_session_id="abcdef123456", name="remote_auth",
        arguments=dict(host="192.0.2.10", port=22, user="fixture",
                       password_env="PRIVATE_MISSING_SSH_PASSWORD")))
    details = shaped["structuredContent"]["error_details"]
    assert details["category"] == "credential_unavailable"
    assert details["submission_state"] == "not_sent"
    assert AUTH_ERROR_HINTS["credential_unavailable"] in shaped["content"][0]["text"]
    assert "PRIVATE_MISSING_SSH_PASSWORD" not in json.dumps(shaped)


def test_auth_maintenance_uncertainty_does_not_change_business_submission(monkeypatch, tmp_path):
    from remote_dev.core.errors import RemoteExecutionError

    error = RemoteExecutionError("PRIVATE_STDERR", category="connection_unavailable",
                                 submission_state="not_sent")
    error.bootstrap = dict(status="unknown", installation="unknown",
                           key_verified=False, error="PRIVATE_STDERR")
    shaped = _remote_failure(monkeypatch, tmp_path, error)
    details = shaped["structuredContent"]["error_details"]
    assert details["submission_state"] == "not_sent"
    assert details["bootstrap"]["installation"] == "unknown"
    assert "PRIVATE_STDERR" not in json.dumps(shaped)


def test_remote_unknown_code_field_and_plain_errors_are_not_promoted(monkeypatch, tmp_path):
    from remote_dev.core.errors import CALLER_ERROR_HINTS, caller_error
    code = next(iter(CALLER_ERROR_HINTS))
    shaped = _remote_failure(monkeypatch, tmp_path, caller_error("x", error_code=code, field="secret_internal"))
    assert "field" not in shaped["structuredContent"]["error_details"]
    shaped = _remote_failure(monkeypatch, tmp_path, caller_error("x", error_code="made_up"))
    assert shaped["structuredContent"]["error_details"].get("error_code") in (None, *CALLER_ERROR_HINTS)
    shaped = _remote_failure(monkeypatch, tmp_path, ValueError("RAW-SECRET"))
    assert shaped["structuredContent"]["error_details"]["submission_state"] != "not_sent"
    assert "RAW-SECRET" not in json.dumps(shaped)


def test_invalid_utf8_preview_stays_within_declared_byte_budget(tmp_path):
    capture = capture_spool.Capture(tmp_path, 4)
    capture.add("out", b"\xff\xff\xff")
    capture.add("err", b"\xff")
    capture.close()
    assert len(capture.stdout().encode("utf-8")) + len(capture.stderr().encode("utf-8")) <= 4
    assert capture.receipt()["stdout"]["preview_truncated"]
    assert capture.receipt()["stderr"]["preview_truncated"]
    assert Path(capture.receipt()["stdout"]["path"]).read_bytes() == b"\xff\xff\xff"


def test_native_components_script_keeps_argument_error_identity(gate):
    import subprocess
    request = dict(jsonrpc="2.0", id=1, method="tools/call", params=dict(
        name="diagnostics_bundle", arguments=dict(root=42), _meta={
            "threadId": "t", "x-codex-turn-metadata": dict(
                thread_id="t", session_id="t", turn_id="turn-1")}))
    result = subprocess.run([sys.executable, str(SCRIPTS / "mcp_gate.py"), "components"],
                            input=json.dumps(request) + "\n", text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    reply = next(json.loads(line) for line in result.stdout.splitlines()
                 if json.loads(line).get("id") == 1)
    detail = reply["result"]["structuredContent"]
    assert detail["code"] == "invalid_arguments"
    assert detail["execution"] == "not_started" and detail["field"] == "root"
