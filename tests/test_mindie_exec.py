"""Component tests for Gate-admitted remote execution. No remote host, NPU, or model."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
DOMAIN = ROOT / "plugins/mindie-agent/domain-lib"
sys.path[:0] = [str(DOMAIN), str(SCRIPTS)]

import mcp_gate  # noqa: E402
import mindie_exec  # noqa: E402
from mindie_target import SshEndpoint  # noqa: E402
from session_gate import Sessions  # noqa: E402


def _reply(structured, *, is_error=False):
    return json.dumps(
        {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": structured,
            "isError": is_error,
        }
    )


def _done(*, exit_code=0, stdout="hello\n", stderr="", job_id="job-done"):
    return {
        "outcome": "success" if exit_code == 0 else "failed",
        "status": "succeeded" if exit_code == 0 else "failed",
        "state": "succeeded" if exit_code == 0 else "failed",
        "job_id": job_id,
        "session_id": job_id,
        "exit_code": exit_code,
        "preview": {"stdout": stdout, "stderr": stderr},
        "bytes_remaining": {"stdout": 0, "stderr": 0},
        "artifacts": [],
    }


class MindieExecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
        self.engine.write_text(json.dumps(dict(root=str(self.root / "data"), domain="test")))
        self.config.write_text(
            json.dumps(dict(python=sys.executable, engine_config=str(self.engine)))
        )
        self.env = patch.dict(
            os.environ,
            {
                "MINDIE_AGENT_CONFIG": str(self.config),
                "CODEX_THREAD_ID": "manual-A",
                "MINDIE_SESSION_ID": "",
                "MINDIE_ACTIVATION": "",
            },
        )
        self.env.start()
        self.sessions = Sessions()
        self.endpoint = SshEndpoint(host="192.0.2.10", port=22, user="root")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def activate(self):
        lease = self.sessions.activate()
        os.environ["MINDIE_SESSION_ID"] = lease["mindie_session_id"]
        os.environ["MINDIE_ACTIVATION"] = lease["mindie_activation"]
        return lease

    def payload(self, run):
        return json.loads(run.call_args.args[1])

    def test_missing_config_fails_closed(self):
        os.environ["MINDIE_AGENT_CONFIG"] = str(self.root / "absent.json")
        with patch.object(mcp_gate, "run") as run:
            with self.assertRaises(mindie_exec.RemoteExecutionError) as caught:
                mindie_exec.ssh_exec(self.endpoint, "true")
        self.assertIn("fail", str(caught.exception).lower())
        run.assert_not_called()

    def test_missing_activation_fails_closed(self):
        with patch.object(mcp_gate, "run") as run:
            with self.assertRaises(mindie_exec.RemoteExecutionError):
                mindie_exec.ssh_exec(self.endpoint, "true")
        run.assert_not_called()

    def test_ssh_exec_uses_preview_and_keeps_completed_job_id(self):
        self.activate()
        with patch.object(mcp_gate, "run", return_value=_reply(_done())) as run:
            proc = mindie_exec.ssh_exec(self.endpoint, "echo hello", timeout=5)
        self.assertIsInstance(proc, subprocess.CompletedProcess)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "hello\n")
        self.assertEqual(proc.stderr, "")
        self.assertEqual(self.payload(run)["name"], "remote_bash")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_check_false_keeps_nonzero_but_not_transport_failure(self):
        self.activate()
        with patch.object(
            mcp_gate, "run", return_value=_reply(_done(exit_code=7, stdout="", stderr="boom"))
        ):
            proc = mindie_exec.ssh_exec(self.endpoint, "false", check=False, timeout=5)
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(proc.stderr, "boom")
        with patch.object(
            mcp_gate,
            "run",
            return_value=json.dumps(
                {"content": [{"type": "text", "text": "auth failed"}], "isError": True}
            ),
        ):
            with self.assertRaises(mindie_exec.RemoteExecutionError):
                mindie_exec.ssh_exec(self.endpoint, "true", check=False, timeout=5)

    def test_truncated_preview_is_drained_or_rejected(self):
        self.activate()
        first = _done()
        first["bytes_remaining"] = {"stdout": 4, "stderr": 0}
        first["preview"] = {"stdout": "he", "stderr": ""}
        second = _done(stdout="llo\n")
        replies = [_reply(first), _reply(second)]

        def fake_run(*args, **kwargs):
            return replies.pop(0)

        with patch.object(mcp_gate, "run", side_effect=fake_run) as run:
            proc = mindie_exec.ssh_exec(self.endpoint, "echo hello", timeout=5)
        self.assertEqual(proc.stdout, "hello\n")
        names = [json.loads(call.args[1])["name"] for call in run.call_args_list]
        self.assertEqual(names, ["remote_bash", "remote_job_stdin"])

        leftover = _done()
        leftover["preview"] = {"stdout": "", "stderr": ""}
        leftover["bytes_remaining"] = {"stdout": 9, "stderr": 0}
        with patch.object(mcp_gate, "run", return_value=_reply(leftover)):
            with self.assertRaises(mindie_exec.RemoteExecutionError) as caught:
                mindie_exec.ssh_exec(self.endpoint, "echo hello", timeout=5)
        self.assertIn("truncated", str(caught.exception).lower())

    def test_running_job_is_not_relaunched(self):
        self.activate()
        running = {
            "outcome": "success",
            "status": "running",
            "state": "running",
            "job_id": "job-live",
            "session_id": "job-live",
            "exit_code": None,
            "preview": {"stdout": "", "stderr": ""},
            "bytes_remaining": {"stdout": 0, "stderr": 0},
        }
        done = _done(job_id="job-live")
        replies = [_reply(running), _reply(done)]

        def fake_run(*args, **kwargs):
            return replies.pop(0)

        with patch.object(mcp_gate, "run", side_effect=fake_run) as run:
            proc = mindie_exec.ssh_exec(self.endpoint, "sleep 1", timeout=5)
        self.assertEqual(proc.returncode, 0)
        names = [json.loads(call.args[1])["name"] for call in run.call_args_list]
        self.assertEqual(names[0], "remote_bash")
        self.assertEqual(names.count("remote_bash"), 1)
        self.assertIn("remote_job_stdin", names)

    def test_unknown_outcome_is_not_retried(self):
        self.activate()
        unknown = {
            "outcome": "failed",
            "status": "unknown",
            "state": "unknown",
            "job_id": "job-lost",
            "preview": {"stdout": "", "stderr": ""},
        }
        with patch.object(mcp_gate, "run", return_value=_reply(unknown)) as run:
            with self.assertRaises(mindie_exec.RemoteExecutionError) as caught:
                mindie_exec.ssh_exec(self.endpoint, "true", check=False, timeout=5)
        self.assertEqual(caught.exception.job_id, "job-lost")
        self.assertIn("not retried", str(caught.exception))
        self.assertEqual(self.payload(run)["name"], "remote_bash")
        self.assertEqual(run.call_count, 1)

    def test_start_job_requires_positive_timeout_and_returns_id(self):
        self.activate()
        with self.assertRaises(TypeError):
            mindie_exec.start_job(self.endpoint, "true", name="x")
        with self.assertRaises(mindie_exec.RemoteExecutionError):
            mindie_exec.start_job(self.endpoint, "true", name="x", timeout=0)
        with patch.object(mcp_gate, "run", return_value=_reply(_done(job_id="job-new"))) as run:
            job = mindie_exec.start_job(
                self.endpoint, "sleep 30", name="bench", timeout=12, env={"A": "1"}
            )
        self.assertEqual(job, "job-new")
        args = self.payload(run)["arguments"]
        self.assertEqual(args["timeout_ms"], 12000)
        self.assertEqual(args["yield_time_ms"], 1000)
        self.assertEqual(args["description"], "bench")
        self.assertEqual(args["env"], {"A": "1"})
        self.assertEqual(run.call_args.kwargs["timeout"], 65)

    def test_job_helpers_use_preview_and_force_and_local_timeout(self):
        self.activate()
        status = {
            "outcome": "success",
            "status": "running",
            "state": "running",
            "job": {"remote_status": {"state": "running"}},
        }
        with patch.object(mcp_gate, "run", return_value=_reply(status)) as run:
            observed = mindie_exec.job_status(self.endpoint, "job-live", timeout=3)
        self.assertEqual(observed["status"], "running")
        self.assertEqual(self.payload(run)["name"], "remote_job_status")
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

        tail = {
            "outcome": "success",
            "status": "ok",
            "preview": {"tail": "line\n", "stderr": ""},
        }
        with patch.object(mcp_gate, "run", return_value=_reply(tail)):
            preview = mindie_exec.job_tail(self.endpoint, "job-live", lines=20)
        self.assertEqual(preview["tail"], "line\n")
        self.assertNotIn("output", preview)

        stopped = {
            "outcome": "cancelled",
            "status": "cancelled",
            "state": "cancelled",
            "job_id": "job-live",
        }
        with patch.object(mcp_gate, "run", return_value=_reply(stopped)) as run:
            result = mindie_exec.job_stop(self.endpoint, "job-live", force=True, timeout=2)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(self.payload(run)["arguments"]["force"])
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_artifacts_require_hash_list_and_default_timeout(self):
        self.activate()
        missing = {"outcome": "success", "status": "ok", "artifacts": []}
        with patch.object(mcp_gate, "run", return_value=_reply(missing)):
            with self.assertRaises(mindie_exec.RemoteExecutionError):
                mindie_exec.artifact_pull(self.endpoint, "/r", str(self.root))
        ok = {
            "outcome": "success",
            "status": "ok",
            "artifacts": [{"sha256": "abc", "pulled": [{"relpath": "f"}]}],
        }
        with patch.object(mcp_gate, "run", return_value=_reply(ok)) as run:
            result = mindie_exec.artifact_push(self.endpoint, str(self.config), "/r")
        self.assertEqual(result["artifacts"][0]["sha256"], "abc")
        self.assertEqual(self.payload(run)["arguments"]["timeout_ms"], 120000)
        self.assertEqual(run.call_args.kwargs["timeout"], 65)

    def test_removed_bypass_apis(self):
        for name in ("ssh_run_bytes", "ssh_argv", "ssh_stream"):
            self.assertFalse(hasattr(mindie_exec, name))

    def test_claim_finish_and_budget_are_reused(self):
        lease = self.activate()
        with patch.object(mcp_gate, "run", return_value=_reply(_done())):
            mindie_exec.ssh_exec(self.endpoint, "true", timeout=5)
        with sqlite3.connect(self.sessions.path) as db:
            rows = db.execute("SELECT kind FROM attempts").fetchall()
            failures = db.execute(
                "SELECT failures FROM leases WHERE session=?", (lease["mindie_session_id"],)
            ).fetchone()
        self.assertTrue(rows)
        self.assertEqual(rows[0][0], "mcp")
        self.assertEqual(failures[0], 0)

    def test_open_local_forward_admits_and_closes(self):
        self.activate()

        class Handle:
            def __init__(self):
                self.closed = False
                self.local_port = 18080
                self.local_host = "127.0.0.1"
                self.owner = None

            def close(self):
                self.closed = True

        handle = Handle()
        with patch(
            "remote_dev.core.ssh_transport.open_local_forward", return_value=handle
        ) as opener:
            with mindie_exec.open_local_forward(self.endpoint, 8000) as fwd:
                self.assertIs(fwd, handle)
            self.assertTrue(handle.closed)
            opener.assert_called_once()
        os.environ["MINDIE_AGENT_CONFIG"] = str(self.root / "absent.json")
        with patch("remote_dev.core.ssh_transport.open_local_forward") as opener:
            with self.assertRaises(mindie_exec.RemoteExecutionError):
                with mindie_exec.open_local_forward(self.endpoint, 8000):
                    pass
        opener.assert_not_called()

    def test_runtime_call_unifies_session_and_state_dir(self):
        self.activate()
        import runtime_call

        captured = {}

        def fake_tool(name, args):
            captured["name"] = name
            captured["state"] = os.environ["REMOTE_DEV_STATE_DIR"]
            captured["session"] = os.environ["REMOTE_DEV_SESSION_ID"]
            return {"text": "ok", "result": {"outcome": "success", "status": "succeeded", "exit_code": 0}}

        with patch("remote_dev.mcp.tools.call_tool", side_effect=fake_tool), patch(
            "remote_dev.core.rpc_transport.close_connections"
        ):
            value = runtime_call.call(
                dict(
                    surface="remote",
                    name="remote_job_status",
                    arguments={"job_id": "job-from-mcp"},
                    mindie_session_id=os.environ["MINDIE_SESSION_ID"],
                    mindie_activation=os.environ["MINDIE_ACTIVATION"],
                )
            )
        self.assertFalse(value["isError"])
        self.assertEqual(captured["session"], "mindie-manual-A")
        self.assertEqual(captured["state"], str(self.root / "data" / "remote-dev"))
        self.assertEqual(captured["name"], "remote_job_status")

    def test_runtime_call_command_failure_is_not_rpc_error(self):
        self.activate()
        import runtime_call

        def fake_tool(name, args):
            return {
                "text": "failed",
                "result": {
                    "outcome": "failed",
                    "status": "failed",
                    "state": "failed",
                    "exit_code": 2,
                    "preview": {"stdout": "", "stderr": "nope"},
                },
            }

        with patch("remote_dev.mcp.tools.call_tool", side_effect=fake_tool), patch(
            "remote_dev.core.rpc_transport.close_connections"
        ):
            value = runtime_call.call(
                dict(
                    surface="remote",
                    name="remote_bash",
                    arguments={"command": "false", "host": "192.0.2.10", "port": 22},
                    mindie_session_id=os.environ["MINDIE_SESSION_ID"],
                    mindie_activation=os.environ["MINDIE_ACTIVATION"],
                )
            )
        self.assertFalse(value["isError"])
        self.assertEqual(value["structuredContent"]["exit_code"], 2)

    def test_mcp_tool_call_without_config_fails_closed(self):
        os.environ["MINDIE_AGENT_CONFIG"] = str(self.root / "absent.json")
        gate = mcp_gate.Gate("remote")
        with patch.object(mcp_gate, "run") as run:
            result = gate.call(
                dict(
                    jsonrpc="2.0",
                    id=1,
                    method="tools/call",
                    params=dict(
                        name="remote_bash",
                        arguments=dict(
                            command="true",
                            mindie_session_id="x",
                            mindie_activation="y",
                        ),
                    ),
                )
            )
        self.assertTrue(result["isError"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
