"""State honesty across runtime dispatch, entry profile validation, and update triggers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import pytest
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.service_host import ServiceHostError

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import component_update  # noqa: E402
import entry  # noqa: E402
import update_lock  # noqa: E402
import update_trigger  # noqa: E402
import windows_process  # noqa: E402


def _isolated_env(tmp_path, config):
    diag = tmp_path / "diag"
    return dict(
        os.environ,
        HOME=str(tmp_path),
        CODEX_HOME=str(tmp_path / "codex"),
        MINDIE_CONFIG_HOME=str(tmp_path / "cfg"),
        MINDIE_DATA_HOME=str(tmp_path / "data"),
        MINDIE_AGENT_CONFIG=str(config),
        MINDIE_DIAGNOSTICS_ROOT=str(diag),
        MINDIE_DIAGNOSTICS_CONFIG=str(diag / "diagnostics.json"),
    )


def test_runtime_call_preserves_not_started_and_service_host_facts(tmp_path):
    admission_file = tmp_path / "admission.sqlite3"
    engine_file = tmp_path / "engine.json"
    config_file = tmp_path / "codex.json"
    engine_file.write_text(
        json.dumps(dict(domain="test", root=str(tmp_path / "corpus"), admission_path=str(admission_file))),
        encoding="utf-8",
    )
    config_file.write_text(
        json.dumps(dict(admission_path=str(admission_file), engine_config=str(engine_file))),
        encoding="utf-8",
    )
    Admission(admission_file).activate("session-1", project_root=str(tmp_path))
    env = _isolated_env(tmp_path, config_file)

    # 1. Pre-RPC malformed payload must stay at stage='request', execution='not_started'.
    bad_json = subprocess.run(
        [sys.executable, str(SCRIPTS / "runtime_call.py")],
        input="{not-json",
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert bad_json.returncode == 0
    bad_shaped = json.loads(bad_json.stdout)
    assert bad_shaped["isError"] is True
    assert bad_shaped["structuredContent"]["stage"] == "request"
    assert bad_shaped["structuredContent"]["execution"] == "not_started"

    # 2. ServiceHostError(uncertain=False) surfaces stage:code and execution='not_started'.
    driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from unittest.mock import patch\n"
        "from mindie_knowledge.loop.service_host import ServiceHostError\n"
        "import runpy\n"
        "with patch('service_client.connect', side_effect=ServiceHostError('interpreter_path_unsupported', stage='interpreter', uncertain=False)):\n"
        f"    runpy.run_path({str(SCRIPTS / 'runtime_call.py')!r}, run_name='__main__')\n"
    )
    query_payload = dict(
        surface="knowledge",
        name="knowledge_query",
        arguments={"query": "ascend"},
        mindie_session_id="session-1",
        native_session_verified=True,
    )
    host_err = subprocess.run(
        [sys.executable, "-I", "-c", driver],
        input=json.dumps(query_payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert host_err.returncode == 0, host_err.stderr
    host_shaped = json.loads(host_err.stdout)
    assert host_shaped["structuredContent"]["stage"] == "service-start"
    assert host_shaped["structuredContent"]["execution"] == "not_started"
    assert host_shaped["structuredContent"]["service_error"] == {
        "stage": "interpreter",
        "code": "interpreter_path_unsupported",
        "startup_uncertain": False,
    }
    assert "interpreter:interpreter_path_unsupported" in host_shaped["content"][0]["text"]

    # 2b. ServiceHostError(uncertain=True) still keeps requested RPC execution='not_started'
    # while surfacing startup_uncertain=True on service_error.
    uncertain_driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from unittest.mock import patch\n"
        "from mindie_knowledge.loop.service_host import ServiceHostError\n"
        "import runpy\n"
        "with patch('service_client.connect', side_effect=ServiceHostError('launch_attempt_mismatch', stage='readiness', uncertain=True)):\n"
        f"    runpy.run_path({str(SCRIPTS / 'runtime_call.py')!r}, run_name='__main__')\n"
    )
    uncertain_err = subprocess.run(
        [sys.executable, "-I", "-c", uncertain_driver],
        input=json.dumps(query_payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    uncertain_shaped = json.loads(uncertain_err.stdout)
    assert uncertain_shaped["structuredContent"]["stage"] == "service-start"
    assert uncertain_shaped["structuredContent"]["execution"] == "not_started"
    assert uncertain_shaped["structuredContent"]["service_error"] == {
        "stage": "readiness",
        "code": "launch_attempt_mismatch",
        "startup_uncertain": True,
    }

    # 3. Read-only knowledge_query RPC failure stays execution='not_started', while mutating
    # knowledge_feedback RPC failure reports execution='outcome_unconfirmed'.
    rpc_driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from unittest.mock import patch\n"
        "import runpy\n"
        "with patch('service_client.connect', return_value=object()), "
        "patch('mindie_knowledge.loop.transport.rpc', side_effect=RuntimeError('rpc failed')):\n"
        f"    runpy.run_path({str(SCRIPTS / 'runtime_call.py')!r}, run_name='__main__')\n"
    )
    read_err = subprocess.run(
        [sys.executable, "-I", "-c", rpc_driver],
        input=json.dumps(query_payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    read_shaped = json.loads(read_err.stdout)
    assert read_shaped["structuredContent"]["stage"] == "operation"
    assert read_shaped["structuredContent"]["execution"] == "not_started"

    mutate_payload = dict(
        surface="knowledge",
        name="knowledge_feedback",
        arguments={"ref": "mindie://test/units/u1@1", "rating": "up"},
        mindie_session_id="session-1",
        native_session_verified=True,
    )
    mutate_err = subprocess.run(
        [sys.executable, "-I", "-c", rpc_driver],
        input=json.dumps(mutate_payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    mutate_shaped = json.loads(mutate_err.stdout)
    assert mutate_shaped["structuredContent"]["stage"] == "operation"
    assert mutate_shaped["structuredContent"]["execution"] == "outcome_unconfirmed"

    # 4. Pre-RPC distill budget error and server-confirmed RequestRejected stay execution='not_started'.
    distill_pre_driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from unittest.mock import patch\n"
        "import runpy\n"
        "with patch('service_client.connect', return_value=object()), "
        "patch('mindie_knowledge.loop.summary_budget.distill_rpc_timeout', side_effect=AttributeError('missing')):\n"
        f"    runpy.run_path({str(SCRIPTS / 'runtime_call.py')!r}, run_name='__main__')\n"
    )
    distill_payload = dict(
        surface="knowledge",
        name="knowledge_distill",
        arguments={"inputs": []},
        mindie_session_id="session-1",
        native_session_verified=True,
    )
    distill_err = subprocess.run(
        [sys.executable, "-I", "-c", distill_pre_driver],
        input=json.dumps(distill_payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    distill_shaped = json.loads(distill_err.stdout)
    assert distill_shaped["structuredContent"]["execution"] == "not_started"

    rejected_driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from unittest.mock import patch\n"
        "from mindie_knowledge.loop.transport import RequestRejected\n"
        "import runpy\n"
        "with patch('service_client.connect', return_value=object()), "
        "patch('mindie_knowledge.loop.transport.rpc', side_effect=RequestRejected('unknown unit ref', error_code='reference_invalid')):\n"
        f"    runpy.run_path({str(SCRIPTS / 'runtime_call.py')!r}, run_name='__main__')\n"
    )
    rejected_err = subprocess.run(
        [sys.executable, "-I", "-c", rejected_driver],
        input=json.dumps(mutate_payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    rejected_shaped = json.loads(rejected_err.stdout)
    assert rejected_shaped["structuredContent"]["stage"] == "operation"
    assert rejected_shaped["structuredContent"]["execution"] == "not_started"
    assert rejected_shaped["structuredContent"]["code"] == "reference_invalid"
    assert "unknown unit ref" in rejected_shaped["structuredContent"]["message"]


def test_update_trigger_lock_contention_does_not_record_fault(tmp_path):
    config = tmp_path / "codex.json"
    config.write_text(json.dumps(dict(python=sys.executable, runtime_scripts=str(SCRIPTS))), encoding="utf-8")
    maintenance = update_trigger.Maintenance(config)
    with update_lock.update_lock(config, exclusive=True, blocking=True):
        before = time.time()
        maintenance._run()
    assert maintenance.last_failure is None
    assert not (update_trigger.directory(config) / "trigger-failure.json").exists()
    assert maintenance.next_attempt >= before + update_trigger.ACTIVATION_BACKOFF - 0.5


def test_entry_malformed_profile_fields_raise_profile_unavailable_and_separate_dispatch(tmp_path):
    config = tmp_path / "codex.json"
    receipt = dict(python=sys.executable, validation=dict(native_version="0.1.30"))
    for bad_adapter in (
        dict(python=sys.executable, runtime_scripts=None, engine_config="/abs/engine.json"),
        dict(python=sys.executable, runtime_scripts=str(SCRIPTS), engine_config=None),
    ):
        with pytest.raises(entry.ProfileUnavailable):
            entry._installed_profile(config, receipt, adapter=bad_adapter)

    # Dispatch failure after profile-binding must NOT record a `profile-binding` stage fault.
    release = tmp_path / "releases/0.1.30"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="0.1.30")
    (release / "runtime.json").write_text(
        json.dumps(dict(python=sys.executable, validation=validation)), encoding="utf-8"
    )
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps(dict(
        domain="test",
        root=str(tmp_path / "corpus"),
        admission_path=str(tmp_path / "admission.sqlite3"),
        transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
        product_validation=validation,
    )), encoding="utf-8")
    config.write_text(json.dumps(dict(
        python=sys.executable,
        runtime_scripts=str(SCRIPTS),
        engine_config=str(engine),
        admission_path=str(tmp_path / "admission.sqlite3"),
        product_validation=validation,
    )), encoding="utf-8")
    env = _isolated_env(tmp_path, config)
    res = subprocess.run(
        [sys.executable, str(SCRIPTS / "entry.py"), "--release", str(release), "bridge", "unknown-op"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert res.returncode == 1
    assert "profile-binding" not in res.stderr


def test_component_update_classifies_json_decode_error_as_catalog_error():
    err = json.JSONDecodeError("Expecting value", "", 0)
    typed = component_update.typed_catalog_error(err)
    assert isinstance(typed, component_update.CatalogError)
    assert typed.code == "release_catalog_invalid"
    assert typed.category == "temporary_network"


def test_windows_assign_job_falls_back_when_breakaway_forbidden():
    import ctypes
    calls = []

    class _Fn:
        def __init__(self, func):
            self._func = func
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self._func(*args)

    class FakeKernel:
        def __init__(self):
            self.CreateJobObjectW = _Fn(lambda _sa, _name: len(calls) + 100)
            self.SetInformationJobObject = _Fn(self._set_info)
            self.AssignProcessToJobObject = _Fn(lambda _job, _proc: 1)
            self.TerminateJobObject = _Fn(lambda _job, _code: 1)
            self.CloseHandle = _Fn(lambda _job: 1)

        def _set_info(self, _job, _cls, info_ptr, _size):
            addr = ctypes.cast(info_ptr, ctypes.c_void_p).value
            flags = int.from_bytes(ctypes.string_at(addr + 16, 4), sys.byteorder)
            calls.append(flags)
            return 0 if (flags & 0x00000800) else 1

    fake_proc = type("Proc", (), {"_handle": 42})()
    with patch("ctypes.WinDLL", return_value=FakeKernel(), create=True), \
            patch("ctypes.get_last_error", return_value=5, create=True), \
            patch("ctypes.WinError", side_effect=OSError, create=True):
        windows_process._assign_job(fake_proc, allow_service=True)
    assert len(calls) == 2
    assert calls[0] & 0x00000800
    assert calls[1] == windows_process._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert fake_proc._mindie_windows_job[1] == 101


def test_runtime_call_preserves_outcome_unconfirmed_on_mutating_request_rejected(tmp_path):
    """A generic RequestRejected during an already-transmitted distill RPC must keep outcome_unconfirmed."""
    admission_file = tmp_path / "admission.sqlite3"
    engine_file = tmp_path / "engine.json"
    config_file = tmp_path / "codex.json"
    engine_file.write_text(
        json.dumps(dict(domain="test", root=str(tmp_path / "corpus"), admission_path=str(admission_file))),
        encoding="utf-8",
    )
    config_file.write_text(
        json.dumps(dict(admission_path=str(admission_file), engine_config=str(engine_file))),
        encoding="utf-8",
    )
    adm = Admission(admission_file)
    adm.activate("session-distill", project_root=str(tmp_path))
    env = _isolated_env(tmp_path, config_file)
    driver = (
        f"import runpy, sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from unittest.mock import patch\n"
        "from mindie_knowledge.loop.transport import RequestRejected\n"
        "with patch('service_client.connect', return_value=object()), "
        "patch('runtime_call._active_lease_token', return_value='tok-1'), "
        "patch('runtime_call.resolve_lease', return_value={'session': 'session-distill'}), "
        "patch('runtime_call.finish_outcome'), "
        "patch('mindie_knowledge.loop.transport.rpc', side_effect=RequestRejected('Expecting value: line 1 column 1 (char 0)')):\n"
        f"    runpy.run_path({str(SCRIPTS / 'runtime_call.py')!r}, run_name='__main__')\n"
    )
    payload = dict(
        surface="knowledge",
        name="knowledge_distill",
        arguments={"inputs": []},
        mindie_session_id="session-distill",
        native_session_verified=True,
    )
    res = subprocess.run(
        [sys.executable, "-I", "-c", driver],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["isError"] is True
    assert out["structuredContent"]["stage"] == "operation"
    assert out["structuredContent"]["execution"] == "outcome_unconfirmed"
    assert out["structuredContent"]["automatic_retry"] is False
    assert "execution=outcome_unconfirmed" in out["content"][0]["text"]
