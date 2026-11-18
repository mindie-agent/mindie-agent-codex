"""Native hook-management CAS and Codex transcript public-material boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mindie-agent"
HOOKS = PLUGIN / "hooks/hooks.json"

MOCK = r'''#!__PYTHON__
import json, os, sys
from pathlib import Path
plugin = Path(os.environ["MOCK_PLUGIN"])
state_path = Path(os.environ["MOCK_STATE"])
state = json.loads(state_path.read_text()) if state_path.is_file() else {}
if sys.argv[1:3] == ["features", "list"]:
    print("mcp_tool_list_changed")
    raise SystemExit(0)
hooks_json = json.loads((plugin / "hooks/hooks.json").read_text(encoding="utf-8"))
rows = []
for event, name in (("SessionStart", "sessionStart"), ("UserPromptSubmit", "userPromptSubmit"), ("Stop", "stop")):
    entry = hooks_json["hooks"][event][0]["hooks"][0]
    command = entry.get("commandWindows", entry["command"]) if os.name == "nt" else entry["command"]
    rows.append({"pluginId": "mindie-agent@mindie-agent", "eventName": name,
                 "sourcePath": str(plugin / "hooks/hooks.json"), "handlerType": "command",
                 "command": command, "key": f"k-{name}",
                 "enabled": not (name == "stop" and os.environ.get("MOCK_STOP_DISABLED") == "1"),
                 "trustStatus": "untrusted", "currentHash": "sha256:" + name})
for name in ("sessionStart", "userPromptSubmit", "stop"):
    state.setdefault("hooks", {}).setdefault("k-" + name, {"trusted_hash": "sha256:" + name,
        "enabled": not (name == "stop" and os.environ.get("MOCK_STOP_DISABLED") == "1")})
config = {"hooks": {"state": state.get("hooks", {})},
          "marketplaces": {"mindie-agent": {"source_type": "git",
             "source": "https://github.com/mindie-agent/mindie-agent-codex.git", "visibility": "public"}},
          "features": {}}
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method, params, result = msg.get("method"), msg.get("params") or {}, {}
    if method == "initialize":
        result = {}
    elif method == "hooks/list":
        result = {"data": [{"hooks": rows, "errors": []}]}
    elif method == "config/read":
        result = {"config": config, "layers": [{"name": {"type": "user",
                  "file": str(Path(os.environ["CODEX_HOME"]) / "config.toml")},
                  "version": 1, "config": config}]}
    elif method == "config/value/write":
        parts = params["keyPath"].split(".")
        if parts[:2] == ["hooks", "state"]:
            key = json.loads(parts[2])
            state.setdefault("hooks", {}).setdefault(key, {})[parts[-1]] = params["value"]
        elif parts[0] == "marketplaces":
            config["marketplaces"][json.loads(parts[1])] = params["value"]
        elif parts[0] == "features":
            config["features"][parts[1]] = params["value"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = {}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
'''


def _mock(tmp_path, monkeypatch, *, stop_disabled=False):
    script = tmp_path / "app-server.py"
    script.write_text(MOCK.replace("__PYTHON__", sys.executable), encoding="utf-8", newline="\n")
    script.chmod(0o755)
    state = tmp_path / "state.json"
    monkeypatch.setenv("MOCK_PLUGIN", str(PLUGIN))
    monkeypatch.setenv("MOCK_STATE", str(state))
    monkeypatch.setenv("MOCK_STOP_DISABLED", "1" if stop_disabled else "0")
    if os.name == "nt":
        cmd = tmp_path / "app-server.cmd"
        cmd.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(cmd)
    return str(script)


def test_native_hooks_trust_and_catalog(tmp_path, monkeypatch):
    from native_hooks import NativeHooks

    native = _mock(tmp_path, monkeypatch)
    env = dict(os.environ)
    with NativeHooks(native, env) as api:
        hooks = api.hooks()
        assert {row["eventName"] for row in hooks} == {"sessionStart", "userPromptSubmit", "stop"}
        config, layer, target = api.user_config()
        assert layer["version"] == 1 and target.name == "config.toml"
        stop = next(row for row in hooks if row["eventName"] == "stop")
        assert api.write_trust(stop, stop["currentHash"]) is True
        assert api.write_trust(stop, "sha256:different") is False
        selected = api.select_catalog(tmp_path / "catalog")
        assert selected == {"status": "selected", "changed": True}
        assert api.enable_catalog_refresh()["status"] == "enabled"


def test_preserve_keeps_disabled_stop_disabled(tmp_path, monkeypatch):
    from native_hooks import NativeHooks, preserve

    native = _mock(tmp_path, monkeypatch, stop_disabled=True)
    env = dict(os.environ)
    with NativeHooks(native, env) as api:
        stop = next(row for row in api.hooks() if row["eventName"] == "stop")
        assert api.write_trust(stop, stop["currentHash"]) is False
    report = preserve(native, env, PLUGIN, [{"key": stop["key"], "event": "stop", "hash": stop["currentHash"]}])
    assert report == {"status": "complete", "trusted": 0, "user_choices_preserved": 1}
    with NativeHooks(native, env) as api:
        assert next(row for row in api.hooks() if row["eventName"] == "stop")["enabled"] is False


def _transcript(home: Path, *, fork: bool = False) -> Path:
    path = home / "sessions/rollout-test.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"id": "sess", "cwd": str(home)}}
    if fork:
        meta["payload"]["forked_from_id"] = "parent"
        meta["timestamp"] = "2026-01-01T00:00:10Z"
        meta["payload"]["timestamp"] = "2026-01-01T00:00:10Z"
    records = [
        {"timestamp": "2026-01-01T00:00:05Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "channel": "final", "phase": "final",
                     "content": [{"type": "input_text", "text": "<environment_context>hidden</environment_context>",
                                  "channel": "final"}]}},
        {"timestamp": "2026-01-01T00:00:20Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "channel": "final", "phase": "final",
                     "content": [{"type": "output_text", "text": "public answer", "channel": "final"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "text": "HIDDEN"}},
    ]
    path.write_text("\n".join(json.dumps(item) for item in [meta, *records]) + "\n", encoding="utf-8", newline="\n")
    return path


def test_transcript_identity_and_material(tmp_path):
    from codex_transcript import capture_source, identify, read_material, same_file

    home = tmp_path / "codex-home"
    path = _transcript(home)
    identity = identify(path)
    assert identity is not None and identity.anchor_len > 0
    assert same_file(identity, identify(path))
    assert capture_source(path, "sess", codex_home=home)["session_id"] == "sess"
    result = read_material(path, 0, session_id="sess")
    assert result["status"] == "ok"
    assert "public answer" in result["text"]
    assert "HIDDEN" not in result["text"] and "<environment_context>" not in result["text"]
    assert result["end"] > 0 and len(result["digest"]) == 64
    path.write_text(path.read_text(encoding="utf-8").replace("public answer", "rewritten answer"), encoding="utf-8", newline="\n")
    assert read_material(path, result["end"], session_id="sess", expected=identity)["status"] == "replaced"


def test_transcript_fork_boundary(tmp_path):
    from codex_transcript import read_material

    home = tmp_path / "codex-home"
    path = _transcript(home, fork=True)
    result = read_material(path, 0, session_id="sess")
    assert "public answer" in result["text"]
    assert "hidden" not in result["text"]
