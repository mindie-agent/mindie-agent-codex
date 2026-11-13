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


@pytest.mark.parametrize(
    "prompt",
    [
        "Download Kimi",
        "Explain /mindie-agent",
        "`/mindie-agent`",
        "Read the mindie-agent source",
        "/mindie-agent-other",
        "[$other-skill](/tmp/SKILL.md) tune KDA",
        "[@other-plugin](plugin://other@mindie-agent) tune KDA",
    ],
)
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


@pytest.mark.parametrize(
    "prompt",
    [
        "/mindie-agent tune KDA",
        "\ufeff/mindie-agent tune KDA",
        "$mindie-agent tune KDA",
        "$mindie-agent:mindie-agent tune KDA",
        "[$mindie-agent](/Users/dev/.codex/skills/mindie-agent/SKILL.md) tune KDA",
        "[$mindie-agent:mindie-agent](C:\\Users\\dev\\.codex\\skills\\mindie-agent\\SKILL.md) tune KDA",
        "[@mindie-agent](plugin://mindie-agent@mindie-agent) tune KDA",
    ],
)
def test_native_entry_grants_following_turns_but_not_other_sessions(tmp_path, prompt):
    path = tmp_path / "admission.sqlite3"
    event = dict(hook_event_name="UserPromptSubmit", session_id="manual", cwd=str(tmp_path), prompt=prompt)
    assert manual_entry.action(event) == "tune KDA"
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
    from tests.process_fixtures import stop_owned_knowledge_service
    def _cleanup():
        if engine.is_file():
            try:
                stop_owned_knowledge_service(engine)
            except Exception:
                return
    request.addfinalizer(_cleanup)
    engine.write_text(json.dumps(dict(
        domain="test", root=str(tmp_path / "corpus"),
        admission_path=str(tmp_path / "admission.sqlite3"),
        transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
        product_validation=validation)))
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, runtime_scripts=str(SCRIPTS), engine_config=str(engine),
        product_validation=validation, admission_path=str(tmp_path / "admission.sqlite3"))))
    environment = dict(os.environ, MINDIE_AGENT_CONFIG=str(config), PLUGIN_DATA=str(tmp_path / "data"),
                       PLUGIN_ROOT=str(SCRIPTS.parent))
    authority = Admission(tmp_path / "admission.sqlite3")
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
    # Production UPS uses `uv --python 3.13 --offline`; this case drives the
    # same launcher with the test interpreter so grant-once is hermetic.
    argv = [sys.executable, str(SCRIPTS / "hook_launcher.py"), "user-prompt-submit"]
    token = None
    for prompt in ("Download Kimi", "/mindie-agent tune KDA", "Continue tuning"):
        event = dict(hook_event_name="UserPromptSubmit", session_id="native-session",
                     turn_id=prompt.split()[0].strip("/"), cwd=str(tmp_path), prompt=prompt)
        result = subprocess.run(argv, input=json.dumps(event), text=True,
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


def test_stage_specific_inactive_diagnostics_prevent_infinite_resend_loop(tmp_path, monkeypatch):
    import bridge
    import knowledge_cli

    diag = tmp_path / "diag"
    config = tmp_path / "codex.json"
    admission_file = tmp_path / "codex.admission.sqlite3"
    release = tmp_path / "releases/0.1.30"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="0.1.30")
    (release / "runtime.json").write_text(
        json.dumps(dict(python=sys.executable, validation=validation)), encoding="utf-8"
    )
    # Corrupt profile (`engine_config: null`) fails at `profile-binding` with ProfileUnavailable.
    config.write_text(
        json.dumps(dict(
            python=sys.executable,
            runtime_scripts=str(SCRIPTS),
            engine_config=None,
            admission_path=str(admission_file),
            product_validation=validation,
        )),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        CODEX_HOME=str(tmp_path / "codex"),
        MINDIE_AGENT_CONFIG=str(config),
        MINDIE_DIAGNOSTICS_ROOT=str(diag),
        MINDIE_DIAGNOSTICS_CONFIG=str(diag / "diagnostics.json"),
    )
    event = dict(
        hook_event_name="UserPromptSubmit",
        session_id="thread-top-1",
        cwd=str(tmp_path),
        prompt="/mindie-agent check status",
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "entry.py"), "--release", str(release), "user-prompt-submit"],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert "profile-binding" in response["systemMessage"]
    assert "ProfileUnavailable" in response["systemMessage"]
    assert "do not repeat /mindie-agent in a loop" in response["systemMessage"]
    assert "Do NOT ask the user to resend /mindie-agent in a loop" in (
        response["hookSpecificOutput"]["additionalContext"]
    )

    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-top-1")
    with pytest.raises(bridge.Inactive) as exc_info:
        bridge.activate("activate")
    message = str(exc_info.value)
    assert "profile-binding" in message
    assert "ProfileUnavailable" in message
    assert "do not repeat /mindie-agent in a loop" in message

    # Independent session B must NOT inherit or misinterpret session A's authorization receipt.
    manual_entry.record_entry_status(
        config, stage="authorize", status="authorized", session_id="thread-top-1"
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "independent-chat-b")
    with pytest.raises(bridge.Inactive) as other_chat_exc:
        bridge.activate("activate")
    assert "session_mismatch" not in str(other_chat_exc.value)
    assert "no /mindie-agent receipt for this session" in str(other_chat_exc.value)
    with pytest.raises(knowledge_cli.KnowledgeInactive) as cli_other_exc:
        knowledge_cli.invoke("describe", {})
    assert "session_mismatch" not in str(cli_other_exc.value)

    # Same session with an authorized hook receipt but no lease in the active store reports admission_mismatch.
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-top-1")
    with pytest.raises(bridge.Inactive) as mismatch_exc:
        bridge.activate("activate")
    assert "admission_mismatch" in str(mismatch_exc.value)


def test_hook_launcher_failure_keeps_ordinary_prompt_passive_and_surfaces_explicit_gesture(tmp_path):
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

    explicit = dict(ordinary, prompt="/mindie-agent inspect operator")
    res_explicit = subprocess.run(
        [sys.executable, str(SCRIPTS / "hook_launcher.py"), "user-prompt-submit"],
        input=json.dumps(explicit),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert res_explicit.returncode == 0
    payload = json.loads(res_explicit.stdout)
    assert "hook-launcher" in payload["systemMessage"]
    assert "JSONDecodeError" in payload["systemMessage"]
    receipt = manual_entry.read_entry_status(config, session_id="thread-1")
    assert receipt["stage"] == "hook-launcher"
    assert receipt["status"] == "failed"

    # Outer POSIX shell hook wrapper & bootstrap.py must keep ordinary prompts {} with zero writes.
    if os.name != "nt":
        import native_entry
        missing_env = dict(
            env,
            CODEX_PLUGIN_ROOT=str(tmp_path / "missing-plugin"),
            PLUGIN_DATA=str(tmp_path / "missing-data"),
        )
        cmd = native_entry.hook_commands("user-prompt-submit")["command"]
        shell_ord = subprocess.run(
            ["/bin/sh", "-c", cmd],
            input=json.dumps(ordinary),
            text=True,
            capture_output=True,
            env=missing_env,
            timeout=10,
        )
        assert shell_ord.returncode == 0
        assert json.loads(shell_ord.stdout) == {}

        shell_exp = subprocess.run(
            ["/bin/sh", "-c", cmd],
            input=json.dumps(explicit),
            text=True,
            capture_output=True,
            env=missing_env,
            timeout=10,
        )
        assert shell_exp.returncode == 0
        assert "UserPromptSubmit launcher failed" in json.loads(shell_exp.stdout)["systemMessage"]

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


@pytest.mark.skipif(os.name != "nt", reason="Windows headless PowerShell 5.1 regression")
def test_windows_headless_powershell_51_user_prompt_submit_utf8_no_console_error(tmp_path, request):
    import windows_process
    from bootstrap import publish_entry
    from tests.process_fixtures import stop_owned_knowledge_service

    ps51 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not ps51.is_file():
        pytest.skip("Windows PowerShell 5.1 is not present")

    release = tmp_path / "data/releases/fixture"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="fixture")
    (release / "runtime.json").write_text(
        json.dumps(dict(python=sys.executable, validation=validation)), encoding="utf-8"
    )
    publish_entry(sys.executable, SCRIPTS, release)
    engine = tmp_path / "engine.json"
    def _cleanup():
        if engine.is_file():
            try:
                stop_owned_knowledge_service(engine)
            except Exception:
                return
    request.addfinalizer(_cleanup)
    admission_file = tmp_path / "admission.sqlite3"
    engine.write_text(json.dumps(dict(
        domain="test",
        root=str(tmp_path / "corpus"),
        admission_path=str(admission_file),
        transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
        product_validation=validation,
    )), encoding="utf-8")
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(
        python=sys.executable,
        runtime_scripts=str(SCRIPTS),
        engine_config=str(engine),
        product_validation=validation,
        admission_path=str(admission_file),
    )), encoding="utf-8")
    commands = native_entry.hook_commands("user-prompt-submit")
    encoded = commands["commandWindows"].split("-EncodedCommand ", 1)[1]
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        CODEX_HOME=str(tmp_path / "codex"),
        MINDIE_AGENT_CONFIG=str(config),
        PLUGIN_DATA=str(tmp_path / "data"),
        PLUGIN_ROOT=str(SCRIPTS.parent),
        MINDIE_DIAGNOSTICS_ROOT=str(tmp_path / "diag"),
        MINDIE_DIAGNOSTICS_CONFIG=str(tmp_path / "diag/diagnostics.json"),
    )
    unicode_cwd = tmp_path / "中文目录"
    unicode_cwd.mkdir()
    event = dict(
        hook_event_name="UserPromptSubmit",
        session_id="win-ps51-session",
        cwd=str(unicode_cwd),
        prompt="\ufeff\u3000/mindie-agent 中文算子分析",
    )
    proc = windows_process.spawn(
        [str(ps51), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(json.dumps(event, ensure_ascii=False), timeout=30)
    finally:
        windows_process.close_tree(proc)
    assert proc.returncode == 0, stderr
    assert "OutputEncoding" not in stderr
    response = json.loads(stdout)
    assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    lease = Admission(admission_file).knowledge_lease("win-ps51-session")
    assert lease is not None
    assert Path(lease["project_root"]).resolve() == unicode_cwd.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX sh outer-hook verification")
def test_outer_hook_preserves_canonical_gesture_and_post_read_failures(tmp_path):
    commands = native_entry.hook_commands("user-prompt-submit")
    # Missing PLUGIN_ROOT forces the outer launcher invocation to fail nonzero after reading or before launch.
    missing_env = dict(
        os.environ,
        HOME=str(tmp_path),
        CODEX_HOME=str(tmp_path / "codex"),
        MINDIE_AGENT_CONFIG=str(tmp_path / "codex.json"),
        PLUGIN_ROOT=str(tmp_path / "missing-plugin-root"),
        PLUGIN_DATA=str(tmp_path / "data"),
        MINDIE_DIAGNOSTICS_ROOT=str(tmp_path / "diag"),
        MINDIE_DIAGNOSTICS_CONFIG=str(tmp_path / "diag/diagnostics.json"),
    )
    for prompt, extra, should_surface in (
        ("/mindie-agent-other", {}, False),
        ("/mindie-agent:other", {}, False),
        ("/Mindie-Agent help", {}, False),
        ("/mindie-agent\ufeffhelp", {}, False),
        ("\b/mindie-agent help", {}, False),
        ("/mindie-agent\bhelp", {}, False),
        ("$mindie-agent:other", {}, False),
        ("ordinary {\"prompt\": \"/mindie-agent\"}", {}, False),
        ("/mindie-agent help", {"agent_id": "sub-1"}, False),
        ("/mindie-agent", {}, True),
        ("\ufeff/mindie-agent help", {}, True),
        ("\u3000/mindie-agent\u00a0help", {}, True),
        ("$mindie-agent:mindie-agent help", {}, True),
        ("[$mindie-agent:mindie-agent](/a/SKILL.md) hi", {}, True),
        ("[@mindie-agent](plugin://mindie-agent@mindie-agent)", {}, True),
    ):
        for ensure_ascii in (False, True):
            payload = dict(hook_event_name="UserPromptSubmit", session_id="s-1", cwd=str(tmp_path), prompt=prompt, **extra)
            res = subprocess.run(
                ["/bin/sh", "-c", commands["command"]],
                input=json.dumps(payload, ensure_ascii=ensure_ascii),
                text=True,
                capture_output=True,
                env=missing_env,
                timeout=10,
            )
            assert res.returncode == 0
            parsed = json.loads(res.stdout)
            if should_surface:
                assert "systemMessage" in parsed, (ensure_ascii, prompt, parsed)
            else:
                assert parsed == {}, (ensure_ascii, prompt, parsed)

    # Injecting SystemExit(1) inside setup.install AFTER entry.py consumes stdin must still surface failure.
    release = tmp_path / "releases/0.1.30"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="0.1.30")
    (release / "runtime.json").write_text(
        json.dumps(dict(python=sys.executable, validation=validation)), encoding="utf-8"
    )
    driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import setup, runpy\n"
        "setup.install = lambda *a, **kw: (_ for _ in ()).throw(SystemExit(1))\n"
        f"sys.argv = [{str(SCRIPTS / 'entry.py')!r}, '--release', {str(release)!r}, 'user-prompt-submit']\n"
        f"runpy.run_path({str(SCRIPTS / 'entry.py')!r}, run_name='__main__')\n"
    )
    run_env = dict(
        os.environ,
        HOME=str(tmp_path),
        CODEX_HOME=str(tmp_path / "codex"),
        MINDIE_AGENT_CONFIG=str(tmp_path / "uncreated/codex.json"),
        MINDIE_DIAGNOSTICS_ROOT=str(tmp_path / "diag"),
        MINDIE_DIAGNOSTICS_CONFIG=str(tmp_path / "diag/diagnostics.json"),
    )
    explicit = dict(hook_event_name="UserPromptSubmit", session_id="s-exit", cwd=str(tmp_path), prompt="/mindie-agent help")
    completed = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps(explicit),
        text=True,
        capture_output=True,
        env=run_env,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    surfaced = json.loads(completed.stdout)
    assert "profile-binding" in surfaced["systemMessage"]
    assert "SystemExit" in surfaced["systemMessage"]

    # A handled helper return code (e.g. dispatch returning 1) must NOT emit a false entry.prepare diagnostic.
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
    handled_res = subprocess.run(
        [sys.executable, "-c", handled_driver],
        text=True,
        capture_output=True,
        env=dict(
            run_env,
            MINDIE_AGENT_CONFIG=str(valid_config),
            MINDIE_DIAGNOSTICS_ROOT=str(clean_diag),
            MINDIE_DIAGNOSTICS_CONFIG=str(clean_diag / "d.json"),
        ),
        timeout=10,
    )
    assert handled_res.returncode == 1
    assert "MindIE release entry failed" not in handled_res.stderr
    assert not list(clean_diag.rglob("*.jsonl"))

    # A Windows exec_or_wait forwarded child SystemExit(1) must NOT manufacture a false bootstrap/entry diagnostic.
    win_diag = tmp_path / "win-diag"
    win_driver = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import bootstrap\n"
        "from pathlib import Path\n"
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
            run_env,
            MINDIE_DIAGNOSTICS_ROOT=str(win_diag),
            MINDIE_DIAGNOSTICS_CONFIG=str(win_diag / "d.json"),
        ),
        timeout=10,
    )
    assert win_res.returncode == 0, win_res.stderr
    assert "MindIE runtime preparation failed" not in win_res.stderr
    assert not list(win_diag.rglob("*.jsonl"))


def test_knowledge_cli_describe_preserves_inactive_stage_and_not_started(tmp_path, monkeypatch, capsys):
    import knowledge_cli

    config = tmp_path / "codex.json"
    admission_file = tmp_path / "admission.sqlite3"
    config.write_text(
        json.dumps(dict(
            python=sys.executable,
            runtime_scripts=str(SCRIPTS),
            admission_path=str(admission_file),
        )),
        encoding="utf-8",
    )
    manual_entry.record_entry_status(
        config,
        stage="profile-binding",
        status="failed",
        session_id="cli-session-1",
        error_type="ProfileUnavailable",
    )
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    monkeypatch.setenv("CODEX_THREAD_ID", "cli-session-1")
    rc = knowledge_cli.main(["describe"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["isError"] is True
    assert out["structuredContent"]["code"] == "knowledge_inactive"
    assert out["structuredContent"]["stage"] == "authorization"
    assert out["structuredContent"]["execution"] == "not_started"
    assert "profile-binding (ProfileUnavailable)" in out["structuredContent"]["message"]
    assert "do not repeat /mindie-agent in a loop" in out["content"][0]["text"]


def test_entry_preserves_committed_authorization_when_receipt_write_times_out(tmp_path):
    release = tmp_path / "releases/0.1.30"
    release.mkdir(parents=True)
    validation = dict(schema="mindie-candidate-check/1", status="validated", native_version="0.1.30")
    (release / "runtime.json").write_text(
        json.dumps(dict(python=sys.executable, validation=validation)), encoding="utf-8"
    )
    admission_file = tmp_path / "adm.sqlite3"
    engine_file = tmp_path / "engine.json"
    config_file = tmp_path / "codex.json"
    engine_file.write_text(
        json.dumps(dict(
            transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
            product_validation=validation,
            admission_path=str(admission_file),
        )),
        encoding="utf-8",
    )
    config_file.write_text(
        json.dumps(dict(
            python=sys.executable,
            runtime_scripts=str(SCRIPTS),
            engine_config=str(engine_file),
            admission_path=str(admission_file),
            product_validation=validation,
        )),
        encoding="utf-8",
    )
    driver = (
        f"import runpy, sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from unittest.mock import patch\n"
        "import bounded_process, manual_entry\n"
        "ok_res = bounded_process.ProcessResult(stdout='', stderr='', returncode=0, execution='completed', cleanup=[])\n"
        f"sys.argv = [{str(SCRIPTS / 'entry.py')!r}, '--release', {str(release)!r}, 'user-prompt-submit']\n"
        "with patch.object(manual_entry, 'record_entry_status', side_effect=TimeoutError('receipt timeout')), "
        "patch.object(bounded_process, 'run', return_value=ok_res) as maint_mock:\n"
        f"    runpy.run_path({str(SCRIPTS / 'entry.py')!r}, run_name='__main__')\n"
        "assert maint_mock.called\n"
    )
    event = dict(
        hook_event_name="UserPromptSubmit",
        session_id="s-receipt-timeout",
        cwd=str(tmp_path),
        prompt="/mindie-agent analyze",
    )
    res = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=dict(
            os.environ,
            HOME=str(tmp_path),
            CODEX_HOME=str(tmp_path / "codex"),
            MINDIE_AGENT_CONFIG=str(config_file),
            MINDIE_DIAGNOSTICS_ROOT=str(tmp_path / "diag"),
            MINDIE_DIAGNOSTICS_CONFIG=str(tmp_path / "diag/d.json"),
        ),
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert set(payload) <= {"systemMessage", "hookSpecificOutput"}
    assert "MindIE session entry succeeded" in payload["systemMessage"]
    assert "local entry receipt update failed (TimeoutError)" in payload["systemMessage"]
    assert "receipt_unconfirmed=true" in payload["hookSpecificOutput"]["additionalContext"]
    assert Admission(admission_file).knowledge_lease("s-receipt-timeout") is not None

    # Ordinary OSError (e.g. PermissionError) during receipt persistence must also surface receipt_unconfirmed=true.
    os_driver = driver.replace("TimeoutError('receipt timeout')", "PermissionError('read-only dir')")
    os_res = subprocess.run(
        [sys.executable, "-c", os_driver],
        input=json.dumps(dict(event, session_id="s-receipt-perm")),
        text=True,
        capture_output=True,
        env=dict(
            os.environ,
            HOME=str(tmp_path),
            CODEX_HOME=str(tmp_path / "codex"),
            MINDIE_AGENT_CONFIG=str(config_file),
            MINDIE_DIAGNOSTICS_ROOT=str(tmp_path / "diag"),
            MINDIE_DIAGNOSTICS_CONFIG=str(tmp_path / "diag/d.json"),
        ),
        timeout=10,
    )
    assert os_res.returncode == 0, os_res.stderr
    os_payload = json.loads(os_res.stdout)
    assert "local entry receipt update failed (PermissionError)" in os_payload["systemMessage"]
    assert "receipt_unconfirmed=true" in os_payload["hookSpecificOutput"]["additionalContext"]
    assert Admission(admission_file).knowledge_lease("s-receipt-perm") is not None

    # Partial multi-domain deactivation must NOT record status='deactivated' or claim deactivation succeeded.
    partial_driver = (
        f"import runpy, sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from unittest.mock import patch\n"
        "import manual_entry\n"
        "from mindie_knowledge.loop import domain_routes\n"
        "partial = {'status': 'partial', 'domains': {'ascendc': 'failed'}}\n"
        f"sys.argv = [{str(SCRIPTS / 'entry.py')!r}, '--release', {str(release)!r}, 'user-prompt-submit']\n"
        "with patch.object(domain_routes, 'deactivate_registered_domains', return_value=partial), "
        "patch.object(manual_entry, 'record_entry_status') as rec_mock:\n"
        f"    runpy.run_path({str(SCRIPTS / 'entry.py')!r}, run_name='__main__')\n"
        "assert not rec_mock.called\n"
    )
    partial_res = subprocess.run(
        [sys.executable, "-c", partial_driver],
        input=json.dumps(dict(event, session_id="s-partial-deact", prompt="/mindie-agent deactivate")),
        text=True,
        capture_output=True,
        env=dict(
            os.environ,
            HOME=str(tmp_path),
            CODEX_HOME=str(tmp_path / "codex"),
            MINDIE_AGENT_CONFIG=str(config_file),
            MINDIE_DIAGNOSTICS_ROOT=str(tmp_path / "diag"),
            MINDIE_DIAGNOSTICS_CONFIG=str(tmp_path / "diag/d.json"),
        ),
        timeout=10,
    )
    assert partial_res.returncode == 0, partial_res.stderr
    partial_payload = json.loads(partial_res.stdout)
    assert "deactivation is incomplete" in partial_payload["systemMessage"]
    assert "deactivation succeeded" not in partial_payload["systemMessage"]
