"""Launcher fast paths, native migration, MCP surfaces, scoped interrupt, and sharing delegation."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mindie-agent"
SCRIPTS = PLUGIN / "scripts"


def _config() -> Path:
    return Path(os.environ["MINDIE_AGENT_CONFIG"])


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8", newline="\n")


def _sharing(root: str, *, enabled: bool = True, choice: str = "local-only") -> None:
    config = _config()
    consent = config.parent / "mindie-consent.json"
    community = config.parent / "mindie-community.json"
    _write_json(consent, {"schema": "mindie-consent/1", "choice": choice, "enabled": enabled,
                          "choice_revision": "a" * 32, "choice_at": time.time(),
                          "project_roots": [root]})
    _write_json(community, {"schema": "mindie-community-config/1", "enabled": enabled,
                            "local_only": (choice == "local-only"),
                            "generation": "gen-1", "enabled_at": time.time(),
                            "project_roots": [root], "consent_config": str(consent)})


def _stdin(monkeypatch, payload: dict | None = None):
    class Stream:
        buffer = io.BytesIO(json.dumps(payload).encode() if payload is not None else b"")

    monkeypatch.setattr(sys, "stdin", Stream())


def _fast(role: str) -> tuple[str, float]:
    import hook_launcher

    output = io.StringIO()
    started = time.perf_counter()
    with redirect_stdout(output):
        code = hook_launcher.fast_path(role, name="mindie-agent", marketplace="mindie-agent",
                                       config=_config(), data=Path(os.environ["PLUGIN_DATA"]))
    elapsed = (time.perf_counter() - started) * 1000
    assert code == 0
    return output.getvalue(), elapsed


def test_stop_fast_paths_without_sqlite(monkeypatch, tmp_path, isolate_host_environment):
    _stdin(monkeypatch)
    assert _fast("stop")[0] == "{}\n"
    _write_json(_config(), {})
    _sharing("/work/app", enabled=False)
    assert _fast("stop")[0] == "{}\n"
    _sharing("/work/app")
    _stdin(monkeypatch, {"cwd": "/work/app-other", "project_root": "/work/app"})
    output, elapsed = _fast("stop")
    assert output == "{}\n" and elapsed < 30
    assert not list(Path(isolate_host_environment).rglob("*.sqlite3"))


def test_user_prompt_and_session_start_fast_paths(monkeypatch, isolate_host_environment):
    class Forbidden:
        @property
        def buffer(self):
            raise AssertionError("user-prompt-submit must not read stdin")

    monkeypatch.setattr(sys, "stdin", Forbidden())
    output, elapsed = _fast("user-prompt-submit")
    assert output == "{}\n" and elapsed < 15
    _write_json(_config(), {"python": sys.executable})
    output, elapsed = _fast("session-start")
    assert output == "{}\n" and elapsed < 15
    marker = Path(os.environ["MINDIE_DATA_HOME"]) / "state/codex/pending-update-check.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == "session_start"
    assert not list(Path(isolate_host_environment).rglob("*.sqlite3"))


def test_cold_session_start_marker_only_without_uv_sync(monkeypatch, isolate_host_environment):
    import bootstrap
    import entry
    import hook_launcher

    assert not _config().exists()
    monkeypatch.setattr(
        bootstrap,
        "ensure_runtime",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("cold session-start must not run bootstrap")),
    )

    out_hl = io.StringIO()
    t0 = time.perf_counter()
    with redirect_stdout(out_hl):
        assert hook_launcher.launch(["session-start"]) == 0
    elapsed_hl = (time.perf_counter() - t0) * 1000
    assert out_hl.getvalue() == "{}\n" and elapsed_hl < 250

    marker = Path(os.environ["MINDIE_DATA_HOME"]) / "state/codex/pending-update-check.json"
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == "session_start"
    assert not _config().exists()
    assert not list(Path(isolate_host_environment).parent.rglob("*.sqlite3"))

    marker.unlink()
    out_ent = io.StringIO()
    t1 = time.perf_counter()
    with redirect_stdout(out_ent):
        assert entry.main(["session-start"]) == 0
    elapsed_ent = (time.perf_counter() - t1) * 1000
    assert out_ent.getvalue() == "{}\n" and elapsed_ent < 250
    assert marker.is_file()
    assert marker == entry._profile(PLUGIN)[1].pending_update_path
    assert not list(Path(isolate_host_environment).parent.rglob("*.sqlite3"))


def test_bootstrap_ensure_runtime_preserves_harness_generations(monkeypatch, tmp_path):
    import bootstrap

    data = tmp_path / "plugin-data"
    gen_marker = data / "runtimes/codex/generations/gen-active/marker.txt"
    gen_marker.parent.mkdir(parents=True, exist_ok=True)
    gen_marker.write_text("keep-me\n", encoding="utf-8", newline="\n")

    monkeypatch.setattr(bootstrap, "_build", lambda source, d, rt: rt.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(bootstrap, "_scripts", lambda py, ver: SCRIPTS)
    monkeypatch.setattr(bootstrap, "_restore_floors", lambda old, new: None)

    _py, scripts, release = bootstrap.ensure_runtime(PLUGIN, data)
    assert scripts == SCRIPTS
    assert (release / "runtime.json").is_file()
    assert gen_marker.is_file() and gen_marker.read_text(encoding="utf-8") == "keep-me\n"


def test_stop_passes_turn_and_notification_identity_kinds(monkeypatch, tmp_path):
    import entry
    import mindie_harness.runtime.knowledge_binding as kb
    import setup

    _handle, profile = entry._profile(PLUGIN)
    captured = []

    orig_stop = kb.run_stop_capture

    def spy_stop_capture(prof, payload, *, plugin_root=None, timeout_sec=6.0):
        captured.append(dict(payload))
        return orig_stop(prof, payload, plugin_root=plugin_root, timeout_sec=timeout_sec)

    monkeypatch.setattr(kb, "run_stop_capture", spy_stop_capture)

    # Provision profile + authorized local-only sharing with choice_revision
    workspace = tmp_path / "proj"
    workspace.mkdir()
    setup.ensure_initialized(profile, python=Path(sys.executable), scripts_dir=SCRIPTS, release_dir=PLUGIN)
    _sharing(str(workspace), enabled=True, choice="local-only")
    rollout = Path(os.environ["CODEX_HOME"]) / "sessions/rollout-1.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(json.dumps({"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z", "payload": {"id": "sess-warm-1", "cwd": str(workspace)}}) + "\n", encoding="utf-8", newline="\n")

    # 1) Turn Stop event -> identity_kind == "turn"
    _stdin(monkeypatch, {
        "hook_event_name": "Stop",
        "session_id": "sess-warm-1",
        "turn_id": "turn-42",
        "cwd": str(workspace),
        "transcript_path": str(rollout),
    })
    out = io.StringIO()
    with redirect_stdout(out):
        assert entry._stop(profile, PLUGIN) == 0
    assert out.getvalue() == "{}\n"
    assert captured[-1]["identity_kind"] == "turn"
    assert captured[-1]["turn_id"] == "turn-42"

    # 2) Notification Stop event (event_id without turn_id) -> identity_kind == "notification" & drops last_assistant_message
    _stdin(monkeypatch, {
        "hook_event_name": "Stop",
        "session_id": "sess-warm-1",
        "event_id": "evt-99",
        "last_assistant_message": "should-be-dropped",
        "cwd": str(workspace),
        "transcript_path": str(rollout),
    })
    out2 = io.StringIO()
    with redirect_stdout(out2):
        assert entry._stop(profile, PLUGIN) == 0
    assert out2.getvalue() == "{}\n"
    assert captured[-1]["identity_kind"] == "notification"
    assert captured[-1]["event_id"] == "evt-99"
    assert "last_assistant_message" not in captured[-1]

    # 3) Missing both turn_id and event_id -> does NOT invent identity_kind="turn"
    _stdin(monkeypatch, {
        "hook_event_name": "Stop",
        "session_id": "sess-warm-1",
        "cwd": str(workspace),
        "transcript_path": str(rollout),
    })
    with redirect_stdout(io.StringIO()):
        assert entry._stop(profile, PLUGIN) == 0
    assert "identity_kind" not in captured[-1]


def test_remote_write_stdin_integer_session_id_passes_router_validation(tmp_path):
    from host_adapter import CodexHostAdapter
    from mindie_harness.mcp.router import MCPRouter
    from mindie_harness.paths import HarnessError

    adapter = CodexHostAdapter(plugin_root=PLUGIN)
    profile = adapter.resolve_profile()
    dispatched = {}

    def spy_handler(ctx, args):
        dispatched["tool"] = ctx.tool_name
        dispatched["args"] = dict(args)
        return adapter.dispatch_tool(ctx, args, profile)

    remote_router = MCPRouter(
        profile,
        surface="remote",
        tools=adapter.mcp_tools("remote", profile),
        handler=spy_handler,
        session_extractor=adapter.extract_session_identity,
    )
    cleaned = remote_router._validate_and_clean_args("remote_write_stdin", {"session_id": 1, "chars": "ls\n"})
    assert cleaned == {"session_id": 1, "chars": "ls\n"}

    res = remote_router.call_tool({
        "jsonrpc": "2.0",
        "id": 77,
        "method": "tools/call",
        "params": {"name": "remote_write_stdin", "arguments": {"session_id": 1, "chars": "ls\n"}},
    })
    assert dispatched == {"tool": "remote_write_stdin", "args": {"session_id": 1, "chars": "ls\n"}}
    assert "forged_identity_argument" not in json.dumps(res)
    assert "schema_missing_field" not in json.dumps(res)

    tools_router = MCPRouter(profile, surface="tools", tools=adapter.mcp_tools("tools", profile))
    with pytest.raises(HarnessError) as exc_info:
        tools_router._validate_and_clean_args("mindie_doctor", {"session_id": "forged"})
    assert exc_info.value.reason == "forged_identity_argument"


def test_migrate_rebinds_components(monkeypatch):
    import entry

    config = _config()
    engine = config.with_name(config.stem + ".engine.json")
    _write_json(engine, {"transcript_adapter": "/old/codex_transcript.py"})
    _write_json(config, {
        "python": sys.executable, "runtime_scripts": "/old/scripts", "component_release": "/old/release",
        "engine_config": str(engine), "admission_path": str(config.parent / "codex.admission.sqlite3"),
        "product_validation": {"schema": "mindie-candidate-check/1", "status": "validated",
                               "native_version": "0.1.23"},
        "component_commands": {"sim": ["/old/python", "-m", "mindie_sim"],
                               "profiler": ["/old/python", "-m", "mindie_profiler"]},
    })
    adapter, _engine, _profile = entry._prepare(None, PLUGIN, True)
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["runtime_scripts"] == str(SCRIPTS)
    assert saved["product_validation"]["native_version"] == "0.1.40"
    assert saved["python"] == sys.executable
    assert len(saved["domain_configs"]) == 9
    assert Path(saved["engine_config"]).is_relative_to(config.parent / "engines/codex")
    for name in ("sim", "profiler"):
        assert saved["component_commands"][name][0] == sys.executable
    assert adapter["source_root"] == str(PLUGIN)


def test_mcp_surface_separation():
    from host_adapter import CodexHostAdapter

    handle = CodexHostAdapter(plugin_root=PLUGIN)
    profile = handle.resolve_profile()
    remote = {tool["name"] for tool in handle.mcp_tools("remote", profile)}
    tools = {tool["name"] for tool in handle.mcp_tools("tools", profile)}
    assert {"remote_probe", "remote_exec_command"} <= remote
    assert not {"remote_BashOutput", "remote_KillShell"} & remote
    assert not any(name.startswith("knowledge_") for name in remote)
    assert {"mindie_doctor", "diagnostics_status", "npu_top"} <= tools
    assert {"knowledge_query", "knowledge_explain", "knowledge_feedback",
            "knowledge_distill", "knowledge_sync"} <= tools


def test_mindie_interrupt_cancels_inflight_request(tmp_path):
    from mindie_harness.mcp.router import MCPRouter
    from mindie_harness.mcp.server import MCPStdioServer
    from mindie_harness.paths import ProfileRef

    profile = ProfileRef.resolve("codex", plugin_root=PLUGIN, data_dir=tmp_path / "data")
    started = threading.Event()

    def handler(ctx, arguments):
        started.set()
        for _ in range(400):
            if ctx.cancel.is_set():
                return {"content": [{"type": "text", "text": "cancelled"}], "isError": False}
            time.sleep(0.005)
        return {"content": [{"type": "text", "text": "timeout"}], "isError": True}

    tools = [{"name": "slow", "description": "slow", "annotations": {"readOnlyHint": True},
              "inputSchema": {"type": "object", "properties": {}}}]
    router = MCPRouter(profile, surface="tools", tools=tools, handler=handler)
    server = MCPStdioServer(router)
    read_fd, write_fd = os.pipe()
    output = io.BytesIO()

    def writer():
        with os.fdopen(write_fd, "wb") as stream:
            stream.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode() + b"\n")
            stream.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "slow", "arguments": {}}}).encode() + b"\n")
            started.wait(5)
            stream.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "mindie_interrupt", "arguments": {}}}).encode() + b"\n")

    thread = threading.Thread(target=writer); thread.start()
    with os.fdopen(read_fd, "rb") as stdin:
        server.serve(stdin=stdin, stdout=output)
    thread.join(5)
    responses = [json.loads(line) for line in output.getvalue().decode().splitlines() if line.strip()]
    slow = next(item for item in responses if item.get("id") == 2)
    assert "cancelled" in slow["result"]["content"][0]["text"]


def test_consistent_codex_json_update_lock_and_scoped_interrupt(tmp_path):
    from host_adapter import CodexHostAdapter, _scope_matches
    from mindie_harness.locks import FileLock
    from mindie_harness.mcp.router import MCPRouter
    import release_activation

    # Strict scope matching invariants:
    assert _scope_matches(("th-1", "tu-1"), ("th-1", "tu-1")) is True
    assert _scope_matches(("th-1", None), ("th-1", "tu-2")) is False
    assert _scope_matches(("th-1", "tu-2"), ("th-1", None)) is True
    assert _scope_matches(("th-1", "tu-1"), (None, "tu-1")) is False
    assert _scope_matches((None, "tu-1"), (None, "tu-1")) is True

    # (1) Verify .codex.json.update.lock is used consistently across scripts
    for script_path in SCRIPTS.glob("*.py"):
        src = script_path.read_text(encoding="utf-8")
        assert "codex.update.lock" not in src
        assert '.with_suffix(".update.lock")' not in src

    config = _config()
    candidate_release = tmp_path / "releases/0.1.40"
    _write_json(config, {"python": "/old/python", "runtime_scripts": "/old/scripts"})
    _write_json(candidate_release / "runtime.json", {
        "key": "k", "python": sys.executable, "source": str(PLUGIN),
        "validation": {"schema": "mindie-candidate-check/1", "status": "validated", "native_version": "0.1.40"},
    })
    expected = release_activation.runtime_binding(json.loads(config.read_text(encoding="utf-8")))
    with FileLock(config.with_name(".codex.json.update.lock"), exclusive=True, timeout=1.0):
        res = release_activation.main(
            ["release_activation.py", str(config), str(candidate_release), "--attempt"],
            stdin=io.BytesIO(json.dumps({"schema": "mindie-activation-attempt/1", "expected_binding": expected}).encode()),
        )
    assert res == {"status": "deferred", "reason": "operation_busy", "execution": "not_started"}

    # (2) Verify scoped mindie_interrupt & schema-error cleanup
    cadapter = CodexHostAdapter(plugin_root=PLUGIN)
    profile = cadapter.resolve_profile()
    all_started = threading.Barrier(4)
    release_survivors = threading.Event()

    def handler(ctx, arguments):
        all_started.wait(timeout=5.0)
        for _ in range(400):
            if ctx.cancel.is_set():
                return {"content": [{"type": "text", "text": "cancelled"}], "isError": False}
            if arguments.get("tag") != "cancel" and release_survivors.is_set():
                return {"content": [{"type": "text", "text": "completed"}], "isError": False}
            time.sleep(0.005)
        return {"content": [{"type": "text", "text": "timeout"}], "isError": True}

    tools = [{"name": "slow", "description": "slow", "annotations": {"readOnlyHint": True},
              "inputSchema": {"type": "object", "properties": {"tag": {"type": "string"}}, "additionalProperties": False}}]
    router = MCPRouter(profile, surface="tools", tools=tools, handler=handler,
                       session_extractor=cadapter.extract_session_identity)
    read_fd, write_fd = os.pipe()
    output = io.BytesIO()

    def writer():
        with os.fdopen(write_fd, "wb") as stream:
            # Schema-rejected call (id=9) must NOT leak in cadapter._scopes
            stream.write(json.dumps({
                "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "slow", "arguments": {"undeclared": 1},
                           "_meta": {"threadId": "th-1", "x-codex-turn-metadata": {"thread_id": "th-1", "session_id": "th-1", "turn_id": "tu-1"}}},
            }).encode() + b"\n")
            stream.write(json.dumps({
                "jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "slow", "arguments": {"tag": "cancel"},
                           "_meta": {"threadId": "th-1", "x-codex-turn-metadata": {"thread_id": "th-1", "session_id": "th-1", "turn_id": "tu-1"}}},
            }).encode() + b"\n")
            # Same thread, unknown turn (th-1, None) must NOT be cancelled by turn-scoped (th-1, tu-1)
            stream.write(json.dumps({
                "jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {"name": "slow", "arguments": {"tag": "keep-no-turn"},
                           "_meta": {"threadId": "th-1"}},
            }).encode() + b"\n")
            stream.write(json.dumps({
                "jsonrpc": "2.0", "id": 12, "method": "tools/call",
                "params": {"name": "slow", "arguments": {"tag": "keep-other-thread"},
                           "_meta": {"threadId": "th-2", "x-codex-turn-metadata": {"thread_id": "th-2", "session_id": "th-2", "turn_id": "tu-1"}}},
            }).encode() + b"\n")
            stream.flush()
            all_started.wait(timeout=5.0)
            stream.write(json.dumps({
                "jsonrpc": "2.0", "id": 13, "method": "tools/call",
                "params": {"name": "mindie_interrupt", "arguments": {"thread_id": "th-1", "turn_id": "tu-1"},
                           "_meta": {"threadId": "th-1"}},
            }).encode() + b"\n")
            stream.flush()
            time.sleep(0.05)
            release_survivors.set()
            time.sleep(0.05)

    thread = threading.Thread(target=writer); thread.start()
    with os.fdopen(read_fd, "rb") as stdin:
        cadapter.serve_mcp(router, server_name="mindie-tools", server_version="0.1.40", stdin=stdin, stdout=output)
    thread.join(5)
    by_id = {row["id"]: row for row in (json.loads(line) for line in output.getvalue().decode().splitlines() if line.strip())}
    assert by_id[9]["result"]["isError"] is True
    assert by_id[10]["result"]["content"][0]["text"] == "cancelled"
    assert by_id[11]["result"]["content"][0]["text"] == "completed"
    assert by_id[12]["result"]["content"][0]["text"] == "completed"
    assert json.loads(by_id[13]["result"]["content"][0]["text"]) == {"interrupted": [10]}
    assert cadapter._scopes == {}


def test_sharing_and_doctor_cli(tmp_path, capsys, monkeypatch):
    import setup

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for args in (["sharing", "--local-only", "--project-root", str(workspace)],
                 ["sharing", "--status"], ["sharing", "--enable", "--project-root", str(workspace)],
                 ["sharing", "--disable"]):
        assert setup.main(args) == 0
        json.loads(capsys.readouterr().out)
    captured = {}

    def fake_run_host(argv):
        captured["argv"] = list(argv)
        return 0

    import mindie_harness.api
    monkeypatch.setattr(mindie_harness.api, "run_host", fake_run_host)
    assert setup._doctor(["--probe-remote", "--cwd", str(workspace), "--timeout", "9"]) == 0
    assert captured["argv"] == ["--host", "codex", "doctor", "--probe-remote",
                                "--cwd", str(workspace), "--timeout", "9"]
    with pytest.raises(SystemExit):
        setup.main(["sharing", "--enable", "--disable"])


def test_isolated_entry_subprocess_and_corrupt_config_guard(tmp_path):
    import subprocess
    import entry

    proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPTS / "entry.py"), "user-prompt-submit"],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0 and proc.stdout == "{}\n"
    corrupt = _config()
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{corrupt-json", encoding="utf-8")
    (corrupt.parent / "codex.engine.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        entry._prepare(None, PLUGIN, False)
    assert corrupt.read_text(encoding="utf-8") == "{corrupt-json"


def test_legacy_release_activation_cas_and_recovery(tmp_path):
    import subprocess
    import entry
    import release_activation

    config = _config()
    legacy_engine = tmp_path / "legacy-release/profile-deadbeef.engine.json"
    _write_json(legacy_engine, {
        "root": str(tmp_path / "knowledge"),
        "domain": "vllm-ascend",
        "admission_path": str(config.parent / "codex.admission.sqlite3"),
        "community_config": str(config.parent / "mindie-community.json"),
        "transcript_adapter": "/old/codex_transcript.py",
    })
    base_cfg = {
        "python": "/old/python",
        "runtime_scripts": "/old/scripts",
        "component_release": str(tmp_path / "legacy-release"),
        "engine_config": str(legacy_engine),
        "domain_configs": {"vllm-ascend": str(legacy_engine)},
        "product_validation": {"schema": "mindie-candidate-check/1", "status": "validated", "native_version": "0.1.23"},
        "component_commands": {},
    }
    expected_stale = release_activation.runtime_binding(base_cfg)
    mutated = dict(base_cfg, component_commands={
        "sim": ["/ext/python", "-I", "-X", "utf8", "-m", "mindie_sim"],
        "profiler": ["/ext/python", "-I", "-X", "utf8", "-m", "mindie_profiler"],
    })
    _write_json(config, mutated)
    candidate_release = tmp_path / "releases/0.1.40"
    _write_json(candidate_release / "runtime.json", {
        "key": "k",
        "python": sys.executable,
        "source": str(PLUGIN),
        "validation": {"schema": "mindie-candidate-check/1", "status": "validated", "native_version": "0.1.40"},
    })
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "release_activation.py"), str(config), str(candidate_release), "--attempt"],
        input=json.dumps({"schema": "mindie-activation-attempt/1", "expected_binding": expected_stale}),
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {
        "execution": "not_started",
        "reason": "installation_changed",
        "status": "deferred",
    }
    assert json.loads(config.read_text(encoding="utf-8")) == mutated
    assert not (config.parent / "codex.engine.json").exists()

    # Subsequent MCP startup migration preserves legacy engine root and provisions 9 content-addressed domains
    entry._prepare(candidate_release, PLUGIN, True)
    recovered = json.loads(config.read_text(encoding="utf-8"))
    recovered_engine_path = Path(recovered["engine_config"])
    recovered_engine = json.loads(recovered_engine_path.read_text(encoding="utf-8"))
    from mindie_harness.component_commands import COMPONENT_MODULES
    assert recovered["product_validation"]["native_version"] == "0.1.40"
    assert len(recovered["domain_configs"]) == 9
    assert recovered_engine_path.is_relative_to(config.parent / "engines/codex")
    assert recovered["component_commands"]["sim"] == [sys.executable, "-I", "-X", "utf8", "-m", COMPONENT_MODULES["sim"]]
    assert recovered["component_commands"]["profiler"] == [sys.executable, "-I", "-X", "utf8", "-m", COMPONENT_MODULES["profiler"]]
    assert recovered_engine["root"] == str(tmp_path / "knowledge")

    # Matching expected_binding activates directly
    _write_json(config, mutated)
    proc_ok = subprocess.run(
        [sys.executable, str(SCRIPTS / "release_activation.py"), str(config), str(candidate_release), "--attempt"],
        input=json.dumps({"schema": "mindie-activation-attempt/1", "expected_binding": release_activation.runtime_binding(mutated)}),
        capture_output=True, text=True, timeout=20,
    )
    assert proc_ok.returncode == 0
    assert json.loads(proc_ok.stdout)["status"] == "active"
    activated = json.loads(config.read_text(encoding="utf-8"))
    assert activated["python"] == sys.executable
    assert len(activated["domain_configs"]) == 9
    assert activated["component_commands"]["sim"][0] == sys.executable
