"""Thin-plugin contract: frozen hooks, LOC ceiling, seed, locator, release metadata."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mindie-agent"
SCRIPTS = PLUGIN / "scripts"
HOOKS_SHA256 = "ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981"


def test_hooks_manifest_byte_frozen():
    payload = (PLUGIN / "hooks/hooks.json").read_bytes()
    assert len(payload) == 25650
    assert hashlib.sha256(payload).hexdigest() == HOOKS_SHA256


def test_scripts_line_ceiling():
    script_files = list(SCRIPTS.glob("*.py"))
    script_lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in script_files)
    assert script_lines <= 1100, f"plugins/mindie-agent/scripts/*.py exceeded 1100 LOC ceiling: {script_lines}"
    files = script_files + [PLUGIN / "__init__.py"]
    files += [path for path in PLUGIN.rglob("*") if path.suffix in {".js", ".mjs", ".cjs"}
              and "tests" not in path.parts]
    lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files)
    assert lines <= 1500, f"thin plugin scripts exceeded the ceiling: {lines}"


def test_bundled_seed_and_host_adapter():
    import mindie_harness.api as api
    from host_adapter import CodexHostAdapter, SEED_SCHEMA, verify_bundled_seed

    info = verify_bundled_seed(PLUGIN)
    assert info["schema"] == SEED_SCHEMA
    assert Path(info["path"]).is_file()
    assert isinstance(CodexHostAdapter(plugin_root=PLUGIN), api.HostAdapter)


def test_mcp_locator_outside_plugin(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "with_runtime.py").write_text("raise RuntimeError('workspace import hijack')\n", encoding="utf-8", newline="\n")
    plugin_root = tmp_path / "empty-plugin"
    plugin_root.mkdir()
    plugin_data = tmp_path / "data"
    plugin_data.mkdir()
    launcher = plugin_data / "hook_launcher.py"
    launcher.write_text((SCRIPTS / "hook_launcher.py").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    servers = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    remote = servers["mindie-remote-dev"]
    assert servers["mindie-remote-dev"]["env"]["REMOTE_DEV_HARNESS_PROFILE"] == "codex"
    for server_cfg in servers.values():
        s_args = list(server_cfg["args"])
        assert "'0.1.40'" in s_args[s_args.index("-c") + 1]
        assert s_args[s_args.index("--seed") + 1] == "0.1.40"
    args = list(remote["args"])
    inline = args[args.index("-c") + 1]
    seed = args[args.index("--seed") + 1]
    assert seed == "0.1.40"
    env = dict(os.environ, PLUGIN_ROOT=str(plugin_root), PLUGIN_DATA=str(plugin_data),
               CODEX_HOME=str(tmp_path / "codex-home"), MINDIE_AGENT_CONFIG=str(tmp_path / "missing.json"),
               PYTHONPATH="")
    result = subprocess.run([sys.executable, "-B", "-c", inline, "--seed", seed, "user-prompt-submit"],
                            cwd=outside, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "{}\n"

    # Also verify fallback to OS default config location (%APPDATA%/mindie-agent/codex.json on Windows)
    # when MINDIE_AGENT_CONFIG and MINDIE_CONFIG_HOME are unset and PLUGIN_DATA has no direct hook_launcher.py.
    empty_data = tmp_path / "empty-data"
    empty_data.mkdir()
    rel_dir = tmp_path / "rel-0.1.40"
    rel_dir.mkdir()
    (rel_dir / "hook_launcher.py").write_text((SCRIPTS / "hook_launcher.py").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    appdata = tmp_path / "win-appdata"
    xdg_cfg = tmp_path / "posix-xdg"
    cfg_target = (appdata / "mindie-agent/codex.json") if os.name == "nt" else (xdg_cfg / "mindie-agent/codex.json")
    cfg_target.parent.mkdir(parents=True, exist_ok=True)
    cfg_target.write_text(json.dumps({"component_release": str(rel_dir), "product_validation": {"native_version": "0.1.40"}}), encoding="utf-8", newline="\n")
    fb_env = {k: v for k, v in os.environ.items() if k not in {"MINDIE_AGENT_CONFIG", "MINDIE_CONFIG_HOME"}}
    fb_env.update(PLUGIN_ROOT=str(plugin_root), PLUGIN_DATA=str(empty_data), CODEX_HOME=str(tmp_path / "codex-home-2"),
                  APPDATA=str(appdata), XDG_CONFIG_HOME=str(xdg_cfg), PYTHONPATH="")
    fb_res = subprocess.run([sys.executable, "-B", "-c", inline, "--seed", seed, "user-prompt-submit"],
                            cwd=outside, env=fb_env, capture_output=True, text=True, timeout=20)
    assert fb_res.returncode == 0, fb_res.stderr
    assert fb_res.stdout == "{}\n"


def test_release_metadata_cli():
    result = subprocess.run([sys.executable, str(SCRIPTS / "setup.py"), "release-metadata",
                             "--source", str(PLUGIN), "--tests", str(ROOT / "tests"),
                             "--revision", "a" * 40], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["schema"] == "mindie-plugin-release/1"
    assert value["version"] == "0.1.40"
    assert value["source"] == {"repository": "mindie-agent/mindie-agent-codex", "sha": "a" * 40}
    assert value["compatibility"]["harness"] == "codex"
