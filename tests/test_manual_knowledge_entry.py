"""Default-on knowledge admission, explicit deactivation, and fast passive Stop/SessionStart."""
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import pytest
from mindie_knowledge.loop.activation import Admission

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import admission_ops  # noqa: E402
import bridge  # noqa: E402
import runtime_call  # noqa: E402
import session_maintenance  # noqa: E402


@pytest.mark.parametrize("state", ["missing", "automatic", "other-chat", "revoked"])
@pytest.mark.parametrize("name,arguments", [
    ("knowledge_query", {"query": "KDA"}),
    ("knowledge_explain", {"ref": "mindie://test/units/kda@1"}),
    ("knowledge_feedback", {"ref": "mindie://test/units/kda@1", "rating": "up"}),
    ("knowledge_distill", {"inputs": [], "task_state": "in_flight"}),
])
def test_native_identity_does_not_authorize_knowledge(tmp_path, monkeypatch, state, name, arguments):
    admission = tmp_path / "admission.sqlite3"
    config = tmp_path / "adapter.json"
    engine = tmp_path / "engine.json"
    adapter = dict(admission_path=str(admission), engine_config=str(engine))
    config.write_text(json.dumps(adapter))
    engine.write_text(json.dumps(dict(root=str(tmp_path / "corpus"), domain="test", admission_path=str(admission))))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    gate = Admission(admission)
    if state == "automatic":
        gate.associate("chat", project_root=str(tmp_path), not_before=1.0)
    elif state == "other-chat":
        gate.activate("parent", project_root=str(tmp_path))
    elif state == "revoked":
        gate.activate("chat", project_root=str(tmp_path))
        gate.deactivate("chat")

    if state == "revoked":
        with patch("service_client.connect") as connect:
            with pytest.raises(runtime_call.KnowledgeInactive, match="deactivated"):
                runtime_call.call(dict(surface="knowledge", name=name, arguments=arguments,
                                       mindie_session_id="chat", native_session_verified=True))
            connect.assert_not_called()
    else:
        rpc_reply = {"status": "noop"} if name == "knowledge_distill" else {"results": []}
        with patch("service_client.connect", return_value={}), \
                patch("mindie_knowledge.loop.transport.rpc", return_value=rpc_reply), \
                patch.object(runtime_call, "finish_outcome"):
            res = runtime_call.call(dict(surface="knowledge", name=name, arguments=arguments,
                                         mindie_session_id="chat", native_session_verified=True))
            assert res["isError"] is False
        if state == "missing" and name in {"knowledge_query", "knowledge_explain"}:
            assert not admission.exists()

    with patch("codex_transcript.capture_source") as transcript:
        result = admission_ops.operation("stop_capture", dict(
            _adapter=adapter, event=dict(session_id="chat", turn_id="turn", transcript_path="/unread.jsonl")))
        assert result == dict(stage="inert", reason="sharing-disabled")
        transcript.assert_not_called()


def test_passive_stop_emits_no_context_and_does_not_inspect_sharing(tmp_path, monkeypatch, capsys):
    import os
    import subprocess

    monkeypatch.setenv("CODEX_THREAD_ID", "ordinary-chat")
    adapter = dict(admission_path=str(tmp_path / "admission.sqlite3"))
    with patch("sharing.read") as sharing, patch.object(bridge, "_read_hook_stdin") as stdin:
        assert bridge.stop(adapter=adapter) == 0
        sharing.assert_not_called()
        stdin.assert_not_called()
    assert capsys.readouterr().out == "{}\n"
    assert not Path(adapter["admission_path"]).exists()

    # Real entry.py subprocess benchmark with held-open stdin (unconfigured & disabled):
    # checks consent BEFORE reading stdin or launching a second interpreter.
    release = tmp_path / "release"
    release.mkdir()
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="0.1.37")
    (release / "runtime.json").write_text(json.dumps(dict(python=sys.executable, validation=validation)))
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps(dict(
        transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
        product_validation=validation)))
    community = tmp_path / "mindie-community.json"
    config = tmp_path / "codex.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, runtime_scripts=str(SCRIPTS),
        engine_config=str(engine), community_config=str(community),
        admission_path=str(tmp_path / "admission.sqlite3"),
        product_validation=validation)))
    env = dict(os.environ, MINDIE_AGENT_CONFIG=str(config))
    for mode in ("unconfigured", "disabled"):
        if mode == "disabled":
            community.write_text(json.dumps(dict(
                schema="mindie-community-config/1", enabled=False, generation=None,
                enabled_at=None, repository=None, branch="main", project_roots=[])))
        durations = []
        for _ in range(5):
            r_fd, w_fd = os.pipe()
            try:
                started = time.monotonic()
                proc = subprocess.run(
                    [sys.executable, "-I", str(SCRIPTS / "entry.py"), "--release", str(release), "stop"],
                    stdin=r_fd, capture_output=True, text=True, env=env, timeout=2.0,
                )
                durations.append(time.monotonic() - started)
            finally:
                os.close(r_fd)
                os.close(w_fd)
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout == "{}\n"
        assert min(durations) < 0.1, f"{mode} entry.py stop took {durations}"
    assert not (tmp_path / "admission.sqlite3").exists()


def test_cold_session_deactivate_persists_for_stop_and_distill(tmp_path, monkeypatch):
    import consent
    import sharing

    admission_file = tmp_path / "admission.sqlite3"
    config = tmp_path / "adapter.json"
    engine = tmp_path / "engine.json"
    consent_file = consent.consent_path_for(config)
    community_file = consent.shared_community_path_for(config)
    consent_file.parent.mkdir(parents=True, exist_ok=True)
    consent.record_choice("contribute", config)
    community_file.write_text(json.dumps(dict(
        schema="mindie-community-config/1", enabled=True, generation="g1",
        enabled_at=1.0, repository="owner/repo", branch="main",
        project_roots=[sharing.canonical_root(str(tmp_path))], idle_seconds=300,
        consent_config=str(consent_file))))
    adapter = dict(
        admission_path=str(admission_file), engine_config=str(engine),
        community_config=str(community_file))
    config.write_text(json.dumps(adapter))
    engine.write_text(json.dumps(dict(
        root=str(tmp_path / "corpus"), domain="test",
        admission_path=str(admission_file), community_config=str(community_file))))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    monkeypatch.setenv("CODEX_THREAD_ID", "cold-revoked")

    assert not admission_file.exists()
    out = admission_ops.operation("deactivate", {})
    assert out["status"] == "inactive"
    assert Admission(admission_file).inspect("cold-revoked")["status"] == "inactive"

    with pytest.raises(runtime_call.KnowledgeInactive, match="deactivated"):
        runtime_call.call(dict(
            surface="knowledge", name="knowledge_distill",
            arguments={"inputs": [], "task_state": "in_flight"},
            mindie_session_id="cold-revoked", native_session_verified=True))

    codex_sessions = tmp_path / "sessions"
    codex_sessions.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    transcript = codex_sessions / "rollout.jsonl"
    transcript.write_text(json.dumps(dict(
        type="session_meta", timestamp="2026-11-18T00:00:00Z",
        payload=dict(id="cold-revoked", cwd=str(tmp_path), timestamp="2026-11-18T00:00:00Z")
    )) + "\n")
    stop_res = admission_ops.operation("stop_capture", dict(
        _adapter=adapter,
        event=dict(
            session_id="cold-revoked", turn_id="t1",
            cwd=str(tmp_path), transcript_path=str(transcript))))
    assert stop_res == dict(stage="inert", reason="task-revoked")


def test_passive_session_start_does_not_start_or_sync_knowledge(tmp_path, monkeypatch, capsys):
    config = tmp_path / "adapter.json"
    admission = tmp_path / "admission.sqlite3"
    config.write_text(json.dumps(dict(
        engine_config=str(tmp_path / "engine.json"), admission_path=str(admission))))
    monkeypatch.setenv("CODEX_THREAD_ID", "ordinary-session")
    with patch.object(session_maintenance, "bind_explicit_config"), \
            patch("diagnostic_maintenance.maintain", return_value={"status": "ok"}), \
            patch("service_client.connect") as connect, patch("feed_sync.sync") as sync:
        session_maintenance.maintain(config)
        connect.assert_not_called()
        sync.assert_not_called()
    assert json.loads(capsys.readouterr().out)["status"] == "inactive"
    assert not admission.exists()


def test_activation_command_cannot_self_authorize_without_native_user_entry(tmp_path, monkeypatch):
    config = tmp_path / "adapter.json"
    path = tmp_path / "admission.sqlite3"
    config.write_text(json.dumps(dict(admission_path=str(path))))
    monkeypatch.setenv("CODEX_THREAD_ID", "ordinary-session")
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    fake_lease = dict(
        status="active",
        mindie_session_id="ordinary-session",
        mindie_activation="tok-1",
        activated_at=1.0,
        project_root=str(tmp_path),
    )
    with patch.object(bridge.Sessions, "activate", return_value=fake_lease) as activate:
        result = bridge.activate("activate")
        activate.assert_called_once()
    assert result["mindie_session_id"] == "ordinary-session"
