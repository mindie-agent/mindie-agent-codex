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


if __name__ == "__main__":
    unittest.main()
