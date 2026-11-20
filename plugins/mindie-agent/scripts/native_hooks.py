"""Use Codex's public config API to carry forward only already-trusted own hooks.
[Role] Inspect user-layer hook trust, preserve approved handlers, select the official local marketplace catalog, and enable native tool-list refresh via `codex app-server`.
[Invariants] Use compare-and-swap on the exact user layer; never auto-approve a hook or overwrite `enabled: false`.
[Timeouts & Locks] Each RPC defaults to a 30s bound; the owned process is killed on close.
[Side-Effect Policy] Mutate only the user's Codex config through app-server CAS.
"""
from __future__ import annotations
import json, os, subprocess, threading, time
from pathlib import Path
_NO_WIN = 0x08000000 if os.name == "nt" else 0
PLUGIN, MARKETPLACE, OFFICIAL_SOURCE = "mindie-agent@mindie-agent", "mindie-agent", "https://github.com/mindie-agent/mindie-agent-codex.git"
EVENTS = {"sessionStart": "SessionStart", "userPromptSubmit": "UserPromptSubmit", "stop": "Stop", "interrupt": "Interrupt"}
def read_json(path, *, limit=65536):
    raw = Path(path).read_bytes()
    if len(raw) > limit: raise ValueError("MindIE metadata exceeds its byte bound")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict): raise ValueError("MindIE metadata must be an object")
    return value
def _plugin_data_dir():
    return os.environ.get("PLUGIN_DATA") or (str(Path(os.environ["CODEX_HOME"]) / "plugins/data" / PLUGIN.replace("@", "-")) if os.environ.get("CODEX_HOME") else None)
class NativeHooks:
    def __init__(self, native, env):
        self.native, self.env, self.home, self.serial = native, env, Path(env["CODEX_HOME"]), 0
        self.process = subprocess.Popen([native, "app-server", "--stdio", "-c", "features.hooks=true"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, creationflags=_NO_WIN)
        try: self.call("initialize", dict(clientInfo=dict(name="mindie-hook-trust", version="1"), capabilities=dict(experimentalApi=True))); self._send({"method": "initialized"})
        except BaseException: self.close(); raise
    def _send(self, value): self.process.stdin.write((json.dumps(value) + "\n").encode()); self.process.stdin.flush()
    def call(self, method, params, *, timeout=30):
        self.serial += 1; self._send(dict(id=self.serial, method=method, params=params)); deadline = time.monotonic() + float(timeout)
        while (remaining := deadline - time.monotonic()) > 0:
            box: list[bytes] = []
            reader = threading.Thread(target=lambda: box.append(self.process.stdout.readline(1024 * 1024 + 1)), daemon=True)
            reader.start(); reader.join(remaining)
            if reader.is_alive(): self.close(); raise TimeoutError("native hook management response is unavailable")
            if not (line := box[0] if box else b""): raise RuntimeError("native hook management stream ended")
            value = json.loads(line)
            if value.get("id") == self.serial:
                if "error" in value: raise RuntimeError(f"native hook management request failed: {value['error']}")
                return value["result"]
        self.close(); raise TimeoutError("native hook management response is unavailable")
    def hooks(self):
        rows = self.call("hooks/list", dict(cwds=[str(self.home)]))["data"]
        if len(rows) != 1 or rows[0].get("errors"): raise ValueError("native hook discovery is incomplete")
        return [value for value in rows[0]["hooks"] if value.get("pluginId") == PLUGIN]
    def user_config(self):
        config, target = self.call("config/read", dict(includeLayers=True, cwd=str(self.home))), (self.home / "config.toml").resolve()
        layers = [row for row in config["layers"] if row["name"].get("type") == "user" and Path(row["name"].get("file", "")).resolve() == target]
        if len(layers) != 1: raise ValueError("native user configuration layer is ambiguous")
        return config, layers[0], target
    def write_trust(self, hook, previous_hash):
        config, layer, target = self.user_config(); state = ((config.get("config", {}).get("hooks") or {}).get("state") or {}).get(hook["key"]) or {}
        approved = f"{state.get('trusted_hash')}/{state['trusted_content_hash']}" if state.get("trusted_content_hash") is not None else state.get("trusted_hash")
        if state.get("enabled") is False or approved != previous_hash: return False
        self.call("config/value/write", dict(keyPath="hooks.state." + json.dumps(hook["key"]) + ".trusted_hash", value=hook["currentHash"], mergeStrategy="replace", expectedVersion=layer["version"], filePath=str(target))); return True
    def select_catalog(self, catalog):
        config, layer, target = self.user_config(); prior = layer.get("config", {}).get("marketplaces", {}).get(MARKETPLACE); selected = dict(source_type="local", source=str(catalog))
        if not isinstance(prior, dict): raise ValueError("native official marketplace configuration is missing")
        if all(prior.get(k) == v for k, v in selected.items()): return dict(status="selected", changed=False)
        if prior.get("source_type") != "git" or prior.get("source") != OFFICIAL_SOURCE: raise ValueError("native marketplace is not the official Git source")
        self.call("config/value/write", dict(keyPath="marketplaces." + json.dumps(MARKETPLACE), value=dict(prior, **selected), mergeStrategy="replace", expectedVersion=layer["version"], filePath=str(target)))
        return dict(status="selected", changed=True)
    def enable_catalog_refresh(self):
        features = subprocess.run([self.native, "features", "list"], capture_output=True, env=self.env, timeout=10, check=False, creationflags=_NO_WIN).stdout.decode("utf-8", "replace")
        if not any(line.split()[:1] == ["mcp_tool_list_changed"] for line in features.splitlines()): return dict(status="unsupported", automatic_refresh=False)
        config, layer, target = self.user_config()
        if (config.get("config", {}).get("features") or {}).get("mcp_tool_list_changed") is True: return dict(status="enabled", changed=False, applies="native_reload_boundary")
        self.call("config/value/write", dict(keyPath="features.mcp_tool_list_changed", value=True, mergeStrategy="replace", expectedVersion=layer["version"], filePath=str(target)))
        return dict(status="enabled", changed=True, applies="native_reload_boundary")
    def close(self):
        process, self.process = getattr(self, "process", None), None
        if process is None: return
        try:
            if process.stdin is not None: process.stdin.close()
        except TimeoutError: raise
        except (OSError, ValueError): pass
        try: process.wait(timeout=0.5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5.0)
    def __enter__(self): return self
    def __exit__(self, kind, error, traceback): self.close()
def configure_catalog_refresh():
    native = os.environ.get("MINDIE_CODEX_BIN") or os.environ.get("CODEX_APP_RUNTIME_CLI_PATH")
    if not native or not os.environ.get("CODEX_HOME"): return dict(status="unavailable", reason="native_management_context_missing", automatic_refresh=False)
    try:
        with NativeHooks(native, dict(os.environ)) as api: return api.enable_catalog_refresh()
    except TimeoutError: raise
    except Exception as exc: return dict(status="failed", error_type=type(exc).__name__, automatic_refresh=False)
def _own_handler(hook, source):
    if (event := EVENTS.get(hook.get("eventName"))) is None: return False
    if Path(hook["sourcePath"]).resolve() != (Path(source) / "hooks/hooks.json").resolve(): raise ValueError("native hook source differs from the selected plugin")
    commands = read_json(Path(source) / "hooks/hooks.json")["hooks"].get(event, [])
    if event == "Interrupt": return hook.get("handlerType") == "mcpTool" and hook.get("tool") == "mindie_interrupt" and hook.get("server") in read_json(Path(source) / ".mcp.json")["mcpServers"]
    if hook.get("handlerType") != "command" or len(commands) != 1 or len(commands[0].get("hooks", [])) != 1: raise ValueError("selected plugin hook envelope is ambiguous")
    value = commands[0]["hooks"][0]; expected = value.get("commandWindows", value.get("command")) if os.name == "nt" else value.get("command")
    if hook.get("command") != expected and hook.get("command") not in _listed_commands(expected, source): raise ValueError("native hook command differs from the installed payload")
    return True
def _listed_commands(command, source):
    forms, data = set(), _plugin_data_dir()
    for root in {str(Path(source)), str(Path(source).resolve())}:
        expanded = command.replace("${PLUGIN_ROOT}", root); forms.add(expanded)
        if data: forms.add(expanded.replace("${PLUGIN_DATA}", data))
    if data: forms.add(command.replace("${PLUGIN_DATA}", data))
    return forms
def snapshot(native, env, source):
    with NativeHooks(native, env) as api:
        return [dict(key=r["key"], event=r["eventName"], hash=r["currentHash"]) for r in api.hooks() if _own_handler(r, source) and r.get("enabled") is True and r.get("trustStatus") == "trusted"]
def preserve(native, env, source, approved):
    expected = {(r["key"], r["event"]): r["hash"] for r in approved}
    if len(expected) != len(approved): raise ValueError("previous native hook approvals are ambiguous")
    if not expected: return dict(status="unchanged", trusted=0)
    with NativeHooks(native, env) as api:
        trusted, preserved = set(), 0; rows = [r for r in api.hooks() if (r["key"], r["eventName"]) in expected]
        if {(r["key"], r["eventName"]) for r in rows} != set(expected): raise ValueError("previously approved native hook is missing")
        for row in rows:
            key = row["key"], row["eventName"]
            if not _own_handler(row, source): raise ValueError("selected native hook identity is ambiguous")
            if row.get("enabled") is not True: preserved += 1
            elif row.get("trustStatus") == "trusted" or api.write_trust(row, expected[key]): trusted.add(key)
            else: preserved += 1
        return dict(status="complete", trusted=len(trusted), user_choices_preserved=preserved)
def reconcile(native, env, source):
    approved = snapshot(native, env, source)
    return preserve(native, env, source, approved) if approved else dict(status="unchanged", trusted=0)
__all__ = ["NativeHooks", "configure_catalog_refresh", "preserve", "reconcile", "snapshot"]
