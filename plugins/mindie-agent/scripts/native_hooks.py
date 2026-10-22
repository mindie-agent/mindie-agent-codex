"""Use Codex's public config API to carry forward only already-trusted own hooks.

[Role]
Communicates with `codex app-server` (`config/read`, `config/batchWrite`) to inspect user-layer plugin/hook trust and atomically update local marketplace catalog and approved MindIE hook handlers (`SessionStart`, `Stop`, `Interrupt`).

[Invariants]
- Uses compare-and-swap (`expectedVersion`) on the exact user config layer (`user_config`).
- Never auto-approves untrusted hooks; only previously approved MindIE hook handlers are carried forward across updates.

[Timeouts & Locks]
- Bounds each `codex app-server` RPC session (`timeout=15.0s`) and kills the owned process tree on exit.

[Side-Effect Policy]
- Mutates the user's Codex config layer only through `codex app-server` `config/batchWrite` compare-and-swap.
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from bounded_process import ProcessResult, ProcessCleanupError, _spawn, _cleanup
from product_contract import read_json

PLUGIN = "mindie-agent@mindie-agent"
MARKETPLACE = "mindie-agent"
OFFICIAL_SOURCE = "https://github.com/mindie-agent/mindie-agent-codex.git"
EVENTS = {"sessionStart": "SessionStart", "stop": "Stop", "interrupt": "Interrupt"}


class NativeHooks:
    def __init__(self, native, env):
        self.home = Path(env["CODEX_HOME"])
        self.serial = 0
        self.responses = queue.Queue()
        self.process = _spawn([native, "app-server", "--stdio", "-c", "features.hooks=true"],
                              subprocess.PIPE, env)
        self.threads = []
        def reader(stream, stdout):
            try:
                total = 0
                while True:
                    line = stream.readline(1024 * 1024 + 1)
                    if not line:
                        break
                    total += len(line)
                    if total > 4 * 1024 * 1024:
                        raise ValueError("native management output exceeded its bound")
                    if stdout:
                        self.responses.put(json.loads(line))
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                self.responses.put(type(exc).__name__)
            finally:
                if stdout:
                    self.responses.put(None)
        for stream, stdout in ((self.process.stdout, True), (self.process.stderr, False)):
            thread = threading.Thread(target=reader, args=(stream, stdout))
            thread.start()
            self.threads.append(thread)
        try:
            self.call("initialize", dict(clientInfo=dict(name="mindie-hook-trust", version="1"),
                                        capabilities=dict(experimentalApi=True)))
            self._send(dict(method="initialized"))
        except BaseException as error:
            try:
                self.close()
                error.native_writer_settled = True
            except Exception as cleanup:
                error.add_note("Native hook management cleanup also failed: " + type(cleanup).__name__)
                error.process_result = getattr(cleanup, "process_result", None)
            raise

    def _send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        self.process.stdin.flush()

    def call(self, method, params):
        self.serial += 1
        identity = self.serial
        self._send(dict(id=identity, method=method, params=params))
        # This bounds a local administrative RPC; it is not a tool/model
        # deadline and never restarts the server or repeats a write.
        deadline = time.monotonic() + 30
        while True:
            try:
                value = self.responses.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError("native hook management response is unavailable") from None
            if not isinstance(value, dict):
                raise RuntimeError("native hook management stream ended")
            if value.get("id") == identity:
                if "error" in value:
                    raise RuntimeError("native hook management request failed")
                return value["result"]
            if time.monotonic() >= deadline:
                raise TimeoutError("native hook management response is unavailable")

    def hooks(self):
        rows = self.call("hooks/list", dict(cwds=[str(self.home)]))["data"]
        if len(rows) != 1 or rows[0].get("errors"):
            raise ValueError("native hook discovery is incomplete")
        return [value for value in rows[0]["hooks"] if value.get("pluginId") == PLUGIN]

    def user_config(self):
        config = self.call("config/read", dict(includeLayers=True, cwd=str(self.home)))
        target = self.home / "config.toml"
        layers = [row for row in config["layers"]
                  if row["name"].get("type") == "user" and row["name"].get("file") == str(target)]
        if len(layers) != 1:
            raise ValueError("native user configuration layer is ambiguous")
        return config, layers[0], target

    def write_trust(self, hook, previous_hash):
        config, layer, target = self.user_config()
        state = config.get("config", {}).get("hooks", {}).get("state", {}).get(hook["key"], {})
        if state.get("enabled") is False or state.get("trusted_hash") != previous_hash:
            return False
        self.call("config/value/write", dict(
            keyPath="hooks.state." + json.dumps(hook["key"]) + ".trusted_hash",
            value=hook["currentHash"], mergeStrategy="replace",
            expectedVersion=layer["version"], filePath=str(target)))
        return True

    def select_catalog(self, catalog):
        """Replace only the official marketplace source via native config CAS."""
        config, layer, target = self.user_config()
        prior = layer.get("config", {}).get("marketplaces", {}).get(MARKETPLACE)
        if not isinstance(prior, dict):
            raise ValueError("native official marketplace configuration is missing")
        selected = dict(source_type="local", source=str(catalog))
        if all(prior.get(key) == value for key, value in selected.items()):
            return dict(status="selected", changed=False)
        if prior.get("source_type") != "git" or prior.get("source") != OFFICIAL_SOURCE:
            raise ValueError("native marketplace is not the official Git source")
        # Keep native-managed fields such as visibility or policy; only source
        # routing changes. Native validation still owns whether the table is valid.
        value = dict(prior, **selected)
        error = None
        try:
            self.call("config/value/write", dict(
                keyPath="marketplaces." + json.dumps(MARKETPLACE),
                value=value, mergeStrategy="replace",
                expectedVersion=layer["version"], filePath=str(target)))
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            error = exc
        _, observed_layer, _ = self.user_config()
        actual = observed_layer.get("config", {}).get("marketplaces", {}).get(MARKETPLACE)
        if actual != value:
            if error is not None:
                raise error
            raise ValueError("native selected marketplace readback differs")
        return dict(status="selected", changed=True, response_lost=error is not None)

    def close(self):
        if self.process is None:
            return
        process, self.process = self.process, None
        result = ProcessResult("unknown")
        if process.stdin is not None:
            try:
                process.stdin.close()
            except TimeoutError:
                raise
            except OSError as exc:
                result.cleanup.append(dict(stage="close_stdin", error_type=type(exc).__name__))
        _cleanup(process, result, threads=self.threads)
        if result.cleanup:
            raise ProcessCleanupError(result)

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        try:
            self.close()
            if error is not None:
                error.native_writer_settled = True
        except Exception as cleanup:
            if error is None:
                raise
            error.add_note("Native hook management cleanup also failed: " + type(cleanup).__name__)
            error.process_result = getattr(cleanup, "process_result", None)


def _own_handler(hook, source):
    event = EVENTS.get(hook.get("eventName"))
    if event is None:
        return False
    path = Path(source) / "hooks/hooks.json"
    if Path(hook["sourcePath"]).resolve() != path.resolve():
        raise ValueError("native hook source differs from the selected plugin")
    commands = read_json(path)[0]["hooks"].get(event, [])
    if event == "Interrupt":
        if hook.get("handlerType") != "mcpTool":
            return False
        if len(commands) != 1 or len(commands[0].get("hooks", [])) != 2:
            raise ValueError("selected plugin interrupt envelope is ambiguous")
        for index, server in enumerate(("mindie-knowledge", "mindie-remote-dev")):
            if hook.get("key") != f"{PLUGIN}:hooks/hooks.json:interrupt:0:{index}":
                continue
            value = commands[0]["hooks"][index]
            if (value.get("type") != "mcp_tool" or value.get("server") != server
                    or value.get("tool") != "mindie_interrupt"
                    or value.get("input") != dict(thread_id="${session_id}", turn_id="${turn_id}")
                    or hook.get("server") != server or hook.get("tool") != value["tool"]
                    or hook.get("timeoutSec") != value.get("timeout")):
                raise ValueError("native interrupt handler differs from the installed payload")
            return True
        raise ValueError("native interrupt identity differs from the installed payload")
    if hook.get("handlerType") != "command":
        return False
    if len(commands) != 1 or len(commands[0].get("hooks", [])) != 1:
        raise ValueError("selected plugin hook envelope is ambiguous")
    value = commands[0]["hooks"][0]
    expected = value.get("commandWindows", value.get("command")) if os.name == "nt" else value.get("command")
    if hook.get("command") != expected:
        raise ValueError("native hook command differs from the installed payload")
    return True


def snapshot(native, env, source):
    with NativeHooks(native, env) as api:
        return [dict(key=row["key"], event=row["eventName"], hash=row["currentHash"])
                for row in api.hooks()
                if _own_handler(row, source)
                and row.get("enabled") is True and row.get("trustStatus") == "trusted"]


def preserve(native, env, source, approved):
    expected = {(row["key"], row["event"]): row["hash"] for row in approved}
    if len(expected) != len(approved):
        raise ValueError("previous native hook approvals are ambiguous")
    if not expected:
        return dict(status="unchanged", trusted=0)
    with NativeHooks(native, env) as api:
        trusted, preserved, selected = {}, 0, {}
        for row in api.hooks():
            key = row["key"], row["eventName"]
            previous = expected.get(key)
            if previous is None:
                continue
            if key in selected or not _own_handler(row, source):
                raise ValueError("selected native hook identity is ambiguous")
            selected[key] = row
        if selected.keys() != expected.keys():
            raise ValueError("previously approved native hook is missing")
        for key, row in selected.items():
            if row.get("enabled") is not True:
                preserved += 1
                continue
            # An already trusted new hash is an idempotent readback after a
            # lost response. Other user changes are never overwritten.
            if row["trustStatus"] == "trusted":
                trusted[key] = row["currentHash"]
                continue
            if api.write_trust(row, expected[key]):
                trusted[key] = row["currentHash"]
            else:
                preserved += 1
        # A successful config write is not proof that the host accepted the
        # new command hash. Read through the same public discovery API.
        remaining = dict(trusted)
        for row in api.hooks():
            key = row["key"], row["eventName"]
            if key in remaining:
                if (not _own_handler(row, source) or row.get("enabled") is not True
                        or row.get("trustStatus") != "trusted"
                        or row.get("currentHash") != remaining[key]):
                    raise ValueError("native hook trust readback differs from the selected payload")
                del remaining[key]
        if remaining:
            raise ValueError("native hook trust readback is incomplete")
        return dict(status="complete", trusted=len(trusted), user_choices_preserved=preserved)
