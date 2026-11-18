"""Silent default-on UserPromptSubmit pass-through and frozen hook compatibility tests."""
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


@pytest.mark.parametrize(
    "prompt",
    [
        "Download Kimi",
        "Help investigate and optimize this Ascend NPU / vLLM-Ascend workload.",
        "Read the mindie-agent source",
        "[$other-skill](/tmp/SKILL.md) tune KDA",
        "[@other-plugin](plugin://other@mindie-agent) tune KDA",
    ],
)
def test_ordinary_prompt_returns_no_context_or_state(tmp_path, prompt):
    event = dict(hook_event_name="UserPromptSubmit", session_id="ordinary", cwd=str(tmp_path), prompt=prompt)
    assert manual_entry.requested(event) is False
    assert manual_entry.authorize({}, event) == {}
    # Real entry dispatch returns before it even resolves an installed profile.
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "entry.py"), "--release", str(tmp_path / "absent"), "user-prompt-submit"],
        input=json.dumps(event), text=True, capture_output=True, timeout=3,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {}
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "prompt",
    [
        "Tune KDA attention kernel",
        "Profile vLLM-Ascend decode latency",
        "Inspect AscendC UB alignment",
    ],
)
def test_native_entry_grants_following_turns_but_not_other_sessions(tmp_path, prompt):
    path = tmp_path / "admission.sqlite3"
    event = dict(hook_event_name="UserPromptSubmit", session_id="silent-session", cwd=str(tmp_path), prompt=prompt)
    assert manual_entry.requested(event) is False
    assert manual_entry.action(event) == ""
    with patch("service_client.connect") as connect:
        response = manual_entry.authorize(dict(admission_path=str(path)), event)
        connect.assert_not_called()
    assert response == {}
    assert not path.exists()
    assert manual_entry.authorize({}, dict(event, agent_id="subagent")) == {}


def test_generated_native_hook_grants_once_and_keeps_ordinary_turns_context_free(tmp_path, request):
    from bootstrap import publish_entry

    release = tmp_path / "data/releases/fixture"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="fixture")
    (release / "runtime.json").write_text(json.dumps(dict(python=sys.executable, validation=validation)))
    publish_entry(sys.executable, SCRIPTS, release)
    engine = tmp_path / "engine.json"
    admission_file = tmp_path / "admission.sqlite3"
    engine.write_text(json.dumps(dict(
        domain="test", root=str(tmp_path / "corpus"),
        admission_path=str(admission_file),
        transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
        product_validation=validation)))
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, runtime_scripts=str(SCRIPTS), engine_config=str(engine),
        product_validation=validation, admission_path=str(admission_file))))
    environment = dict(os.environ, MINDIE_AGENT_CONFIG=str(config), PLUGIN_DATA=str(tmp_path / "data"),
                       PLUGIN_ROOT=str(SCRIPTS.parent))
    commands = native_entry.hook_commands("user-prompt-submit")
    production = commands["command"]
    assert "hook_launcher.py" in production
    assert "--offline" in production
    assert "--no-python-downloads" in production
    assert commands["timeout"] == native_entry.USER_PROMPT_TIMEOUT
    if os.name == "nt":
        import base64
        windows = commands["commandWindows"]
        assert "-EncodedCommand" in windows
        body = base64.b64decode(windows.split()[-1]).decode("utf-16le")
        assert "hook_launcher.py" in body
        assert "--offline" in body
        assert "--no-python-downloads" in body
    argv = [sys.executable, str(SCRIPTS / "hook_launcher.py"), "user-prompt-submit"]
    for idx, prompt in enumerate(("Download Kimi", "Optimize vLLM-Ascend MLA", "Continue tuning")):
        event = dict(hook_event_name="UserPromptSubmit", session_id="native-session",
                     turn_id=f"turn-{idx}", cwd=str(tmp_path), prompt=prompt)
        result = subprocess.run(argv, input=json.dumps(event), text=True,
                                capture_output=True, env=environment, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {}
        assert not admission_file.exists()


def test_windows_extended_interpreter_aliases_match_across_launcher_and_contract(tmp_path):
    import hook_launcher
    import product_contract

    plain = r"C:\Users\Alice\.codex\plugins\data\mindie-agent\runtimes\abc\Scripts\python.exe"
    extended = r"\\?\C:\users\alice\.codex\plugins\data\mindie-agent\runtimes\abc\Scripts\python.exe"
    forward = "C:/Users/Alice/.codex/plugins/data/mindie-agent/runtimes/abc/Scripts/python.exe"
    unc_plain = r"\\server\share\runtimes\Scripts\python.exe"
    unc_extended = r"\\?\UNC\SERVER\SHARE\runtimes\Scripts\python.exe"

    for checker in (product_contract.same_interpreter, hook_launcher._same_interpreter):
        assert checker(plain, extended, windows=True)
        assert checker(plain, forward, windows=True)
        assert checker(unc_plain, unc_extended, windows=True)
        assert not checker(plain, r"C:\Other\python.exe", windows=True)
        assert not checker(None, plain, windows=True)
        assert not checker("", plain, windows=True)

    release = tmp_path / "releases/0.1.30"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="0.1.30")
    (release / "runtime.json").write_text(
        json.dumps(dict(python=extended, validation=validation)), encoding="utf-8"
    )
    (release / "entry.py").write_text("# fixture\n", encoding="utf-8")
    adapter = dict(python=plain, product_validation=validation)
    orig_same = hook_launcher._same_interpreter
    with patch.object(hook_launcher, "_same_interpreter",
                      side_effect=lambda a, b: orig_same(a, b, windows=True)):
        assert hook_launcher._prepared_entry(adapter, tmp_path) == release / "entry.py"


def test_hook_launcher_and_bootstrap_keep_all_prompts_passive_without_sqlite(tmp_path):
    diag = tmp_path / "diag"
    config = tmp_path / "codex.json"
    config.write_text("{corrupt-json", encoding="utf-8")
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        CODEX_HOME=str(tmp_path / "codex"),
        PLUGIN_DATA=str(tmp_path / "data"),
        MINDIE_AGENT_CONFIG=str(config),
        MINDIE_DIAGNOSTICS_ROOT=str(diag),
        MINDIE_DIAGNOSTICS_CONFIG=str(diag / "diagnostics.json"),
    )
    ordinary = dict(
        hook_event_name="UserPromptSubmit",
        session_id="thread-1",
        cwd=str(tmp_path),
        prompt="Refactor local helper",
    )
    res_ordinary = subprocess.run(
        [sys.executable, str(SCRIPTS / "hook_launcher.py"), "user-prompt-submit"],
        input=json.dumps(ordinary),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert res_ordinary.returncode == 0
    assert json.loads(res_ordinary.stdout) == {}
    assert not config.with_suffix(".entry-status.json").exists()

    boot_diag = tmp_path / "boot-diag"
    boot_env = dict(
        env,
        MINDIE_DIAGNOSTICS_ROOT=str(boot_diag),
        MINDIE_DIAGNOSTICS_CONFIG=str(boot_diag / "diagnostics.json"),
    )
    boot_ord = subprocess.run(
        [sys.executable, str(SCRIPTS / "bootstrap.py"), str(tmp_path / "unwritable/file"), "user-prompt-submit"],
        input=json.dumps(ordinary),
        text=True,
        capture_output=True,
        env=boot_env,
        timeout=10,
    )
    assert boot_ord.returncode == 0
    assert json.loads(boot_ord.stdout) == {}
    assert not boot_diag.exists()


def test_handled_helper_returncode_and_forwarded_child_emit_no_false_diagnostics(tmp_path):
    release = tmp_path / "releases/0.1.30"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="0.1.30")
    (release / "runtime.json").write_text(
        json.dumps(dict(python=sys.executable, validation=validation)), encoding="utf-8"
    )
    clean_diag = tmp_path / "clean-diag"
    valid_engine = tmp_path / "valid-engine.json"
    valid_config = tmp_path / "valid-codex.json"
    valid_engine.write_text(
        json.dumps(dict(
            transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
            product_validation=validation,
        )),
        encoding="utf-8",
    )
    valid_config.write_text(
        json.dumps(dict(
            python=sys.executable,
            runtime_scripts=str(SCRIPTS),
            engine_config=str(valid_engine),
            admission_path=str(tmp_path / "adm.sqlite3"),
            product_validation=validation,
        )),
        encoding="utf-8",
    )
    handled_driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import bounded_process, runpy\n"
        "from unittest.mock import patch\n"
        "res = bounded_process.ProcessResult(stdout='{\"status\":\"degraded\"}\\n', stderr='', returncode=1, execution='completed', cleanup=[])\n"
        f"sys.argv = [{str(SCRIPTS / 'entry.py')!r}, '--release', {str(release)!r}, 'status']\n"
        "with patch.object(bounded_process, 'run', return_value=res):\n"
        f"    runpy.run_path({str(SCRIPTS / 'entry.py')!r}, run_name='__main__')\n"
    )
    base_env = dict(
        os.environ,
        HOME=str(tmp_path),
        CODEX_HOME=str(tmp_path / "codex"),
    )
    handled_res = subprocess.run(
        [sys.executable, "-c", handled_driver],
        text=True,
        capture_output=True,
        env=dict(
            base_env,
            MINDIE_AGENT_CONFIG=str(valid_config),
            MINDIE_DIAGNOSTICS_ROOT=str(clean_diag),
            MINDIE_DIAGNOSTICS_CONFIG=str(clean_diag / "d.json"),
        ),
        timeout=10,
    )
    assert handled_res.returncode == 1
    assert "MindIE release entry failed" not in handled_res.stderr
    assert not list(clean_diag.rglob("*.jsonl"))

    win_diag = tmp_path / "win-diag"
    win_driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import bootstrap\n"
        "fwd = SystemExit(1)\n"
        "fwd._mindie_forwarded_child = True\n"
        "try:\n"
        "    with bootstrap.preparing('bootstrap'):\n"
        "        raise fwd\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 1 and getattr(exc, '_mindie_forwarded_child', False)\n"
    )
    win_res = subprocess.run(
        [sys.executable, "-c", win_driver],
        text=True,
        capture_output=True,
        env=dict(
            base_env,
            MINDIE_DIAGNOSTICS_ROOT=str(win_diag),
            MINDIE_DIAGNOSTICS_CONFIG=str(win_diag / "d.json"),
        ),
        timeout=10,
    )
    assert win_res.returncode == 0, win_res.stderr
    assert "MindIE runtime preparation failed" not in win_res.stderr
    assert not list(win_diag.rglob("*.jsonl"))


def test_entry_user_prompt_submit_returns_empty_json_without_status_files(tmp_path, capsys):
    config = tmp_path / "codex.json"
    config.write_text("{}", encoding="utf-8")
    args = SimpleNamespace(role="user-prompt-submit", arguments=[])
    assert entry.dispatch(args, config) == 0
    assert capsys.readouterr().out == "{}\n"
    assert not config.with_suffix(".entry-status.json").exists()
