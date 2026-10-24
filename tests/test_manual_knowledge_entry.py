"""Knowledge denial precedes service, corpus, model and transcript access."""
import json
from pathlib import Path
import sys
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
    with patch("service_client.connect") as connect:
        with pytest.raises(runtime_call.KnowledgeInactive, match="/mindie-agent"):
            runtime_call.call(dict(surface="knowledge", name=name, arguments=arguments,
                                   mindie_session_id="chat", native_session_verified=True))
        connect.assert_not_called()
    with patch("sharing.read") as settings, patch("codex_transcript.capture_source") as transcript:
        result = admission_ops.operation("stop_capture", dict(
            _adapter=adapter, event=dict(session_id="chat", turn_id="turn", transcript_path="/unread.jsonl")))
        assert result == dict(stage="inert", reason="manual-entry-required")
        settings.assert_not_called()
        transcript.assert_not_called()
    assert not (tmp_path / "corpus").exists()


def test_passive_stop_emits_no_context_and_does_not_inspect_sharing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", "ordinary-chat")
    adapter = dict(admission_path=str(tmp_path / "admission.sqlite3"))
    with patch("sharing.read") as sharing, patch.object(bridge, "_read_hook_stdin") as stdin:
        assert bridge.stop(adapter=adapter) == 0
        sharing.assert_not_called()
        stdin.assert_not_called()
    assert capsys.readouterr().out == "{}\n"
    assert not Path(adapter["admission_path"]).exists()


def test_passive_session_start_does_not_start_or_sync_knowledge(tmp_path, monkeypatch, capsys):
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(
        engine_config=str(tmp_path / "engine.json"), admission_path=str(tmp_path / "admission.sqlite3"))))
    monkeypatch.setenv("CODEX_THREAD_ID", "ordinary-session")
    with patch.object(session_maintenance, "bind_explicit_config"), \
            patch("diagnostic_maintenance.maintain", return_value={"status": "ok"}), \
            patch("service_client.connect") as connect, patch("feed_sync.sync") as sync:
        session_maintenance.maintain(config)
        connect.assert_not_called()
        sync.assert_not_called()
    assert json.loads(capsys.readouterr().out)["status"] == "inactive"


def test_activation_command_cannot_self_authorize_without_native_user_entry(tmp_path, monkeypatch):
    config = tmp_path / "adapter.json"
    path = tmp_path / "admission.sqlite3"
    config.write_text(json.dumps(dict(admission_path=str(path))))
    monkeypatch.setenv("CODEX_THREAD_ID", "ordinary-session")
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    with patch.object(bridge.Sessions, "activate") as activate:
        with pytest.raises(bridge.Inactive, match="not authorized"):
            bridge.activate("activate")
        activate.assert_not_called()
    assert not path.exists()
