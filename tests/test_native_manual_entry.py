"""Native gestures grant a session once; ordinary turns remain context-free."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from unittest.mock import patch

import pytest
from mindie_knowledge.loop.activation import Admission

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import manual_entry  # noqa: E402
import native_entry  # noqa: E402
import entry  # noqa: E402


@pytest.mark.parametrize("prepare_failure", [False, True])
def test_native_dispatch_prepares_the_committed_manual_session(tmp_path, capsys, prepare_failure):
    path = tmp_path / "admission.sqlite3"
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(admission_path=str(path))))
    # Exercise the actual launcher prompt as a native event, including its prefix.
    manifest = json.loads((SCRIPTS.parent / ".codex-plugin/plugin.json").read_text())
    prompt = manifest["interface"]["defaultPrompt"]
    event = dict(hook_event_name="UserPromptSubmit", session_id="manual",
                 cwd=str(tmp_path), prompt=prompt)
    args = SimpleNamespace(role="user-prompt-submit", prompt_event=event, arguments=[])

    def prepare(_config, *, session_id):
        assert session_id == "manual"
        assert Admission(path).knowledge_lease(session_id)
        if prepare_failure:
            raise TimeoutError("private-path")

    with patch.object(entry, "session_start", side_effect=prepare) as preparation:
        entry.dispatch(args, config)
        preparation.assert_called_once_with(config, session_id="manual")
    result = json.loads(capsys.readouterr().out)
    assert bool(result.get("systemMessage")) == prepare_failure
    assert "private-path" not in json.dumps(result)
    assert Admission(path).knowledge_lease("manual")
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps(dict(domain="test", root=str(tmp_path / "corpus"), admission_path=str(path))))
    config.write_text(json.dumps(dict(admission_path=str(path), engine_config=str(engine))))
    args.prompt_event = dict(event, prompt="/mindie-agent deactivate")
    with patch.object(entry, "session_start") as preparation:
        entry.dispatch(args, config)
        preparation.assert_not_called()
    assert Admission(path).knowledge_lease("manual") is None


@pytest.mark.parametrize("prompt", ["Download Kimi", "Explain /mindie-agent", "`/mindie-agent`", "Read the mindie-agent source"])
def test_ordinary_prompt_returns_no_context_or_state(tmp_path, prompt):
    event = dict(hook_event_name="UserPromptSubmit", session_id="ordinary", cwd=str(tmp_path), prompt=prompt)
    assert manual_entry.authorize({}, event) == {}
    # Real entry dispatch returns before it even resolves an installed profile.
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "entry.py"), "--release", str(tmp_path / "absent"), "user-prompt-submit"],
        input=json.dumps(event), text=True, capture_output=True, timeout=3,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {}
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("prompt", ["/mindie-agent tune KDA", "$mindie-agent tune KDA", "$mindie-agent:mindie-agent tune KDA"])
def test_native_entry_grants_following_turns_but_not_other_sessions(tmp_path, prompt):
    path = tmp_path / "admission.sqlite3"
    event = dict(hook_event_name="UserPromptSubmit", session_id="manual", cwd=str(tmp_path), prompt=prompt)
    with patch("service_client.connect") as connect:
        response = manual_entry.authorize(dict(admission_path=str(path)), event)
        connect.assert_not_called()
    assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    original = Admission(path).knowledge_lease("manual")
    assert original
    assert manual_entry.authorize({}, dict(event, prompt="Continue tuning")) == {}
    assert Admission(path).knowledge_lease("manual")["token"] == original["token"]
    assert Admission(path).knowledge_lease("child") is None
    assert manual_entry.authorize({}, dict(event, agent_id="subagent")) == {}


def test_native_deactivation_stops_the_same_session_flywheel(tmp_path):
    path = tmp_path / "admission.sqlite3"
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps(dict(domain="test", root=str(tmp_path / "corpus"), admission_path=str(path))))
    adapter = dict(admission_path=str(path), engine_config=str(engine))
    event = dict(hook_event_name="UserPromptSubmit", session_id="manual", cwd=str(tmp_path), prompt="/mindie-agent")
    manual_entry.authorize(adapter, event)
    original = Admission(path).knowledge_lease("manual")
    assert original
    manual_entry.authorize(adapter, dict(event, prompt="/mindie-agent deactivate"))
    assert Admission(path).knowledge_lease("manual") is None
    assert Admission(path).capture_authorization("manual", original["token"])["state"] == "inactive"


def test_generated_native_hook_grants_once_and_keeps_ordinary_turns_context_free(tmp_path, request):
    from bootstrap import publish_entry
    release = tmp_path / "data/releases/fixture"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="fixture")
    (release / "runtime.json").write_text(json.dumps(dict(python=sys.executable, validation=validation)))
    publish_entry(sys.executable, SCRIPTS, release)
    engine = tmp_path / "engine.json"
    from tests.process_fixtures import public_engine_config, stop_owned_knowledge_service
    request.addfinalizer(lambda: stop_owned_knowledge_service(engine))
    engine.write_text(json.dumps(public_engine_config(
        tmp_path / "corpus", admission_path=str(tmp_path / "admission.sqlite3"),
        product_validation=validation)))
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, runtime_scripts=str(SCRIPTS), engine_config=str(engine),
        product_validation=validation, admission_path=str(tmp_path / "admission.sqlite3"))))
    environment = dict(os.environ, MINDIE_AGENT_CONFIG=str(config), PLUGIN_DATA=str(tmp_path / "data"),
                       PLUGIN_ROOT=str(SCRIPTS.parent))
    authority = Admission(tmp_path / "admission.sqlite3")
    commands = native_entry.hook_commands("user-prompt-submit")
    command = commands["commandWindows" if os.name == "nt" else "command"]
    token = None
    for prompt in ("Download Kimi", "/mindie-agent tune KDA", "Continue tuning"):
        event = dict(hook_event_name="UserPromptSubmit", session_id="native-session",
                     turn_id=prompt.split()[0].strip("/"), cwd=str(tmp_path), prompt=prompt)
        result = subprocess.run(command, shell=True, input=json.dumps(event), text=True,
                                capture_output=True, env=environment, timeout=15)
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        if prompt.startswith("/mindie-agent"):
            assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
            token = authority.knowledge_lease("native-session")["token"]
        else:
            assert response == {}
            lease = authority.knowledge_lease("native-session")
            assert (lease["token"] if lease else None) == token


def test_native_deactivation_preserves_partial_domain_outcomes(tmp_path):
    path = tmp_path / "admission.sqlite3"
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps(dict(domain="test", root=str(tmp_path / "corpus"), admission_path=str(path))))
    bad_target = tmp_path / "target.json"
    bad_target.write_text("{broken")
    adapter = dict(admission_path=str(path), engine_config=str(engine), domain_configs={"operators": str(bad_target)})
    event = dict(hook_event_name="UserPromptSubmit", session_id="manual", cwd=str(tmp_path), prompt="/mindie-agent")
    manual_entry.authorize(adapter, event)
    result = manual_entry.authorize(adapter, dict(event, prompt="/mindie-agent deactivate"))
    assert result["systemMessage"]
    assert Admission(path).knowledge_lease("manual") is None
    assert bad_target.read_text() == "{broken"
