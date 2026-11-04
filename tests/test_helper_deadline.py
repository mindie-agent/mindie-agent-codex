"""remote_write helper deadlines stay inside the outer budget and stay classifiable.

AD mindie-agent#307: mcp.remote.remote_write spent the 180s helper floor and was
recorded as helper_timeout / TimeoutError, with no stack frames. These tests
do not sleep for that budget.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import bounded_process
import mcp_gate
import runtime_call
from tests.process_fixtures import cleanup_temporary_directory


MARKER = "mindie-deadline-fixture-7f3a"


def _lingering(marker):
    listed = subprocess.run(
        ["ps", "-ax", "-o", "pid=,command="],
        capture_output=True, text=True, check=False,
    )
    return [line for line in listed.stdout.splitlines() if marker in line and "ps -ax" not in line]


class _Poll:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


class DeadlineClassificationTests(unittest.TestCase):
    def test_exited_helper_is_not_reported_as_still_running(self):
        error = bounded_process.classified_deadline(_Poll(1), b"")
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(error.category, "helper_output_stalled")
        self.assertFalse(error.helper_alive)
        self.assertNotIn("1", str(error))

    def test_unreachable_stderr_is_a_category_not_a_copied_message(self):
        stderr = b"ssh: connect to host example.invalid port 22: Connection refused\n"
        error = bounded_process.classified_deadline(_Poll(None), stderr)
        self.assertEqual(error.category, "remote_unreachable")
        self.assertTrue(error.helper_alive)
        self.assertNotIn("example.invalid", str(error))
        self.assertNotIn("Connection refused", str(error))

    def test_plain_timeout_word_is_not_treated_as_unreachable(self):
        error = bounded_process.classified_deadline(_Poll(None), b"still timed out\n")
        self.assertEqual(error.category, "helper_still_running")
        self.assertTrue(error.helper_alive)


class HelperRunTests(unittest.TestCase):
    def setUp(self):
        self.marker = MARKER + "-" + self.id().rsplit(".", 1)[-1]

    def tearDown(self):
        self.assertEqual(_lingering(self.marker), [])

    def _run(self, code, timeout, **kwargs):
        started = time.monotonic()
        try:
            return bounded_process.run(
                [sys.executable, "-c", code], "", timeout=timeout, **kwargs,
            )
        finally:
            self.elapsed = time.monotonic() - started

    def test_unreachable_stderr_is_classified_at_the_deadline(self):
        code = (
            "import sys,time\n"
            "sys.stderr.write('ssh: connect to host example.invalid port 22: Connection refused\\n')\n"
            "sys.stderr.flush()\n"
            "time.sleep(30)  # " + self.marker + "\n"
        )
        with self.assertRaises(bounded_process.HelperDeadline) as caught:
            self._run(code, 0.4, classify_timeout=True)
        self.assertGreaterEqual(self.elapsed, 0.3)
        self.assertLess(self.elapsed, 2.0)
        self.assertEqual(caught.exception.category, "remote_unreachable")
        self.assertTrue(caught.exception.helper_alive)
        self.assertNotIn("example.invalid", str(caught.exception))

    def test_authentication_failure_is_classified_at_the_deadline(self):
        code = (
            "import sys,time\n"
            "sys.stderr.write('Permission denied (publickey).\\n')\n"
            "sys.stderr.flush()\n"
            "time.sleep(30)  # " + self.marker + "\n"
        )
        with self.assertRaises(bounded_process.HelperDeadline) as caught:
            self._run(code, 0.4, classify_timeout=True)
        self.assertGreaterEqual(self.elapsed, 0.3)
        self.assertLess(self.elapsed, 2.0)
        self.assertEqual(caught.exception.category, "remote_authentication")
        self.assertNotIn("publickey", str(caught.exception))

    def test_silent_helper_is_still_running_at_the_deadline(self):
        code = "import time\ntime.sleep(30)  # " + self.marker + "\n"
        with self.assertRaises(bounded_process.HelperDeadline) as caught:
            self._run(code, 0.4, classify_timeout=True)
        self.assertGreaterEqual(self.elapsed, 0.3)
        self.assertLess(self.elapsed, 2.0)
        self.assertEqual(caught.exception.category, "helper_still_running")
        self.assertTrue(caught.exception.helper_alive)
        self.assertIsInstance(caught.exception, TimeoutError)

    def test_bare_timed_out_text_does_not_abort_early(self):
        code = (
            "import sys,time\n"
            "sys.stderr.write('still timed out\\n')\n"
            "sys.stderr.flush()\n"
            "time.sleep(30)  # " + self.marker + "\n"
        )
        with self.assertRaises(bounded_process.HelperDeadline) as caught:
            self._run(code, 0.4, classify_timeout=True)
        self.assertGreaterEqual(self.elapsed, 0.3)
        self.assertEqual(caught.exception.category, "helper_still_running")

    def test_unclassified_deadline_stays_a_plain_timeout(self):
        code = "import time\ntime.sleep(30)  # " + self.marker + "\n"
        with self.assertRaises(TimeoutError) as caught:
            self._run(code, 0.3)
        self.assertNotIsInstance(caught.exception, bounded_process.HelperDeadline)
        self.assertFalse(hasattr(caught.exception, "category"))
        self.assertLess(self.elapsed, 2.0)


class RemoteWriteBudgetTests(unittest.TestCase):
    def test_remote_write_inner_deadline_fits_inside_the_helper_budget(self):
        outer = mcp_gate.remote_timeout("remote_write", {})
        self.assertEqual(outer, mcp_gate.REMOTE_MIN_BUDGET_SECONDS)
        args = runtime_call.apply_remote_deadline("remote_write", {"content": "x"}, outer)
        inner = args["timeout_ms"] / 1000.0
        self.assertGreater(inner, 0)
        self.assertLessEqual(
            inner + mcp_gate.REMOTE_GRACE_SECONDS + mcp_gate.REMOTE_RPC_FOLLOW_SECONDS,
            outer,
        )
        self.assertNotEqual(args["timeout_ms"], None)

    def test_native_polls_and_explicit_timeouts_are_not_shortened(self):
        bash = runtime_call.apply_remote_deadline("remote_bash", {"command": "true"}, 660)
        self.assertNotIn("timeout_ms", bash)
        stdin = runtime_call.apply_remote_deadline(
            "remote_write_stdin", {"session_id": 1}, 360,
        )
        self.assertNotIn("timeout_ms", stdin)
        explicit = runtime_call.apply_remote_deadline(
            "remote_write", {"content": "x", "timeout_ms": 200000}, 180,
        )
        self.assertEqual(explicit["timeout_ms"], 200000)
        yielded = runtime_call.apply_remote_deadline(
            "remote_read", {"yield_time_ms": 1000}, 180,
        )
        self.assertNotIn("timeout_ms", yielded)

    def test_budget_smaller_than_grace_is_rejected(self):
        with self.assertRaises(ValueError):
            runtime_call.apply_remote_deadline("remote_write", {"content": "x"}, 10)

    def test_helper_injects_the_deadline_and_direct_calls_do_not(self):
        seen = []

        def dispatch(name, args):
            seen.append((name, dict(args)))
            return {"text": "wrote", "result": {"outcome": "success", "status": "ok"}}

        payload = {
            "surface": "remote",
            "remote_session_id": "task-A",
            "name": "remote_write",
            "arguments": {"file_path": "/tmp/x", "content": "x"},
            "helper_timeout_seconds": 180.0,
        }
        with patch("remote_dev.mcp.tools.call_tool", side_effect=dispatch), \
                patch("remote_dev.core.rpc_transport.close_connections"):
            result = runtime_call.call(payload)
            self.assertFalse(result["isError"])
            payload.pop("helper_timeout_seconds")
            runtime_call.call(payload)
        self.assertEqual(seen[0][0], "remote_write")
        self.assertEqual(
            seen[0][1]["timeout_ms"],
            int((180.0 - mcp_gate.REMOTE_GRACE_SECONDS - mcp_gate.REMOTE_RPC_FOLLOW_SECONDS) * 1000),
        )
        self.assertNotIn("helper_timeout_seconds", seen[0][1])
        self.assertNotIn("timeout_ms", seen[1][1])


class RemoteWriteGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "config.json"
        self.config.write_text(json.dumps({
            "python": sys.executable,
            "runtime_scripts": str(SCRIPTS),
            "engine_config": "/absent/not-read",
        }))
        diag = root / "diagnostics"
        diag.mkdir()
        (diag / "diagnostics.json").write_text('{"decision":"disabled"}\n')
        self.env = patch.dict(os.environ, {
            "MINDIE_AGENT_CONFIG": str(self.config),
            "MINDIE_REMOTE_STATE_DIR": str(root / "remote"),
            "MINDIE_DIAGNOSTICS_CONFIG": str(diag / "diagnostics.json"),
            "MINDIE_DIAGNOSTICS_ROOT": str(diag),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        cleanup_temporary_directory(self.temp)

    def request(self):
        return {
            "id": 1,
            "params": {
                "name": "remote_write",
                "arguments": {
                    "content": "x",
                    "host": "example.invalid",
                    "port": 22,
                },
                "_meta": {
                    "threadId": "task-A",
                    "x-codex-turn-metadata": {
                        "thread_id": "task-A",
                        "session_id": "task-A",
                        "turn_id": "turn-1",
                    },
                },
            },
        }

    def test_gate_passes_the_outer_budget_into_the_helper(self):
        helper = {
            "content": [{"type": "text", "text": "wrote"}],
            "structuredContent": {"outcome": "success", "status": "ok"},
            "isError": False,
        }
        with patch.object(
            mcp_gate, "run",
            return_value=bounded_process.ProcessResult("completed", json.dumps(helper), 0),
        ) as dispatch:
            result = mcp_gate.Gate("remote").call(self.request())
        self.assertFalse(result["isError"])
        payload = json.loads(dispatch.call_args.args[1])
        self.assertEqual(payload["helper_timeout_seconds"], dispatch.call_args.kwargs["timeout"])
        self.assertGreaterEqual(payload["helper_timeout_seconds"], 180.0)
        self.assertNotIn("timeout_ms", payload["arguments"])
        self.assertTrue(dispatch.call_args.kwargs["classify_timeout"])

    def _failed(self, exc):
        with patch.object(mcp_gate, "run", side_effect=exc), \
                patch.object(mcp_gate, "diagnostic_failure", return_value={"logging_failed": False}) as recorded:
            result = mcp_gate.Gate("remote").call(self.request())
        return result, recorded

    def test_unreachable_deadline_replaces_opaque_timeout(self):
        error = bounded_process.HelperDeadline("remote_unreachable", helper_alive=True)
        result, recorded = self._failed(error)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "remote_unreachable")
        self.assertEqual(result["structuredContent"]["stage"], "helper_run")
        self.assertFalse(result["structuredContent"]["automatic_retry"])
        self.assertEqual(result["structuredContent"]["execution"], "outcome_unconfirmed")
        text = " ".join(item["text"] for item in result["content"])
        self.assertIn("unreachable", text)
        self.assertNotIn("deadline exceeded (timeout)", text)
        self.assertEqual(recorded.call_args.args[1], "helper_run")
        self.assertEqual(recorded.call_args.args[2], "remote_unreachable")

    def test_still_running_helper_is_named_in_the_result(self):
        error = bounded_process.HelperDeadline("helper_still_running", helper_alive=True)
        result, recorded = self._failed(error)
        self.assertEqual(result["structuredContent"]["code"], "helper_still_running")
        text = " ".join(item["text"] for item in result["content"])
        self.assertIn("still alive", text)
        self.assertEqual(recorded.call_args.args[2], "helper_still_running")

    def test_plain_timeout_error_keeps_helper_timeout(self):
        result, recorded = self._failed(TimeoutError("stalled"))
        self.assertEqual(result["structuredContent"]["code"], "helper_timeout")
        self.assertEqual(recorded.call_args.args[2], "helper_timeout")


if __name__ == "__main__":
    unittest.main()
