import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"


class AdapterTests(unittest.TestCase):
    def bridge(self, operation, event, config):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), operation],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            timeout=3,
            env={**os.environ, "MINDIE_AGENT_CONFIG": str(config)},
        )

    def test_missing_configuration_drops_stop_without_writes(self):
        with tempfile.TemporaryDirectory() as root:
            start = time.monotonic()
            result = self.bridge(
                "stop", {"session_id": "a"}, Path(root) / "absent.json"
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {})
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_retired_session_start_operation_is_rejected_without_state(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config.json"
            config.write_text("{}")
            result = self.bridge(
                "session-start",
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "exact-test-id",
                    "transcript_path": "/missing/private",
                },
                config,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(list(Path(root).iterdir()), [config])

    def test_worker_uses_fresh_ephemeral_execution_with_hooks_disabled(self):
        spec = importlib.util.spec_from_file_location(
            "agent_worker", SCRIPTS / "agent_worker.py"
        )
        worker = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            spec.loader.exec_module(worker)

        def fake_run(command, prompt):
            self.assertIn("--ephemeral", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("features.hooks=false", command)
            self.assertIn("untrusted task data", prompt)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text('{"verdict":"unknown","reason":"No actual use evidence"}')
            return subprocess.CompletedProcess(command, 0, stdout="")

        with patch.object(worker, "run_codex", fake_run):
            result = worker.run({"role": "judge", "outcome": "untrusted material"})
        self.assertEqual(result["verdict"], "unknown")


if __name__ == "__main__":
    unittest.main()
