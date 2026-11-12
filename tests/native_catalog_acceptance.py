"""Deterministic native Codex Responses/MCP acceptance without model spending."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

NATIVE = (os.environ.get("MINDIE_CODEX_NATIVE") or os.environ.get("MINDIE_TEST_CODEX_BIN")
          or os.environ.get("CODEX_APP_RUNTIME_CLI_PATH") or shutil.which("codex"))
if not NATIVE:
    raise SystemExit("native Codex CLI is required")
FIXTURE = Path(__file__).parent / "fixtures/native_catalog_server.py"
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


def run(enabled):
    bodies = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            body = json.loads(raw)
            bodies.append(body)
            identity = f"resp_{len(bodies)}"
            events = [{"type": "response.created", "response": {"id": identity}}]
            if len(bodies) == 1:
                events.append({"type": "response.output_item.done", "output_index": 0, "item": {
                    "type": "function_call", "id": "fc_refresh", "call_id": "call_refresh",
                    "name": "refresh_probe", "namespace": "mcp__refresh", "arguments": "{}",
                }})
            else:
                events.append({"type": "response.output_item.done", "output_index": 0, "item": {
                    "type": "message", "id": "msg_done", "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                }})
            events.append({"type": "response.completed", "response": {
                "id": identity, "status": "completed", "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }})
            payload = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="mindie-native-same-turn-") as directory:
        root = Path(directory)
        for generation in ("before", "after"):
            scripts = root / generation
            scripts.mkdir()
            (scripts / "runtime_call.py").write_bytes(FIXTURE.read_bytes())
        adapter = root / "adapter.json"
        adapter.write_text(json.dumps({
            "python": sys.executable, "runtime_scripts": str(root / "before"),
        }), encoding="utf-8", newline="\n")
        updates = adapter.with_suffix(".updates")
        updates.mkdir()
        (updates / "status.json").write_text(json.dumps({
            "checked_at": time.time(), "last_successful_check": time.time(), "status": "current",
        }), encoding="utf-8", newline="\n")
        env = dict(os.environ)
        for key in ("HOME", "USERPROFILE", "CODEX_HOME", "MINDIE_CONFIG_HOME", "MINDIE_DATA_HOME", "MINDIE_DIAGNOSTICS_ROOT"):
            (root / key).mkdir()
            env[key] = str(root / key)
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            env.pop(key, None)
        env.pop("PYTHONPATH", None)
        config = (
            'model = "gpt-5.3-codex"\nmodel_provider = "fixture"\n'
            '[features]\nmcp_tool_list_changed = ' + str(enabled).lower() + '\n'
            '[model_providers.fixture]\nname = "fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{http.server_port}/v1"\n'
            '[mcp_servers.refresh]\ncommand = ' + json.dumps(sys.executable)
            + '\nargs = ' + json.dumps([str(SCRIPTS / "mcp_gate.py"), "components"]) + '\n'
            + '[mcp_servers.refresh.env]\nMINDIE_AGENT_CONFIG = ' + json.dumps(str(adapter)) + '\n'
        )
        (Path(env["CODEX_HOME"]) / "config.toml").write_text(config, encoding="utf-8", newline="\n")
        proc = subprocess.run(
            [NATIVE, "exec", "--json", "--skip-git-repo-check", "--sandbox", "danger-full-access",
             "Call the refresh_probe tool once then say done."],
            env=env, cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result = dict(enabled=enabled, exit_code=proc.returncode, requests=bodies,
                      stdout=proc.stdout, stderr=proc.stderr)
        assert (root / "calls.jsonl").exists(), proc.stdout[-2000:] + proc.stderr[-2000:]
        result["helper_calls"] = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    http.shutdown()
    return result


if __name__ == "__main__":
    results = [run(False), run(True)]
    report = []
    for result in results:
        descriptions = [
            [child["description"] for tool in request.get("tools", [])
             if tool.get("name") == "mcp__refresh" for child in tool["tools"]
             if child["name"] == "refresh_probe"]
            for request in result["requests"]
        ]
        expected = [["before"], ["after" if result["enabled"] else "before"]]
        assert result["exit_code"] == 0, result["stderr"]
        assert descriptions == expected, descriptions
        events = [json.loads(line) for line in result["stdout"].splitlines()]
        calls = [event for event in events if event.get("type") == "item.completed"
                 and event.get("item", {}).get("type") == "mcp_tool_call"]
        assert len(calls) == 1 and calls[0]["item"]["status"] == "completed", calls
        assert result["helper_calls"] == [{"generation": "before", "name": "refresh_probe"}]
        report.append(dict(enabled=result["enabled"], descriptions=descriptions,
                           native_tool_calls=1, inference_requests=2, paid_model_calls=0,
                           same_turn=True, actual_mindie_facade=True))
    print(json.dumps(report))
