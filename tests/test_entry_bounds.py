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

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
spec = importlib.util.spec_from_file_location(
    "process_guard", SCRIPTS / "process_guard.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class EntryBoundsTests(unittest.TestCase):
    def test_invalid_hooks_never_invoke_runtime(self):
        event = dict(
            hook_event_name="Stop",
            session_id="s",
            turn_id="t",
            last_assistant_message="Done",
        )
        cases = [
            dict(event, stop_hook_active=True),
            dict(event, hook_event_name="PreToolUse"),
            dict(event, session_id="bad\ncontext"),
            dict(event, last_assistant_message="x" * 32769),
            [],
            None,
            dict(event, extra="x" * 131072),
        ]
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "called"
            runtime = Path(directory) / "runtime"
            runtime.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            runtime.chmod(0o755)
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(dict(python=str(runtime), engine_config="unused"))
            )
            for case in cases:
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / "bridge.py"), "stop"],
                    input=json.dumps(case),
                    text=True,
                    capture_output=True,
                    env={**os.environ, "MINDIE_AGENT_CONFIG": str(config)},
                    timeout=3,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout), {})
                self.assertFalse(marker.exists())

    def test_unknown_entry_cannot_dispatch_engine_operation(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), "serve"],
            capture_output=True,
            timeout=2,
        )
        self.assertEqual(result.returncode, 1)

    def test_worker_stops_on_error_tool_or_extra_turn(self):
        cases = [
            [{"type": "error", "message": "Reconnecting"}],
            [{"type": "item.started", "item": {"type": "command_execution"}}],
            [{"type": "item.started", "item": {"type": "new_unknown_tool"}}],
            [{"type": "turn.started"}, {"type": "turn.started"}],
        ]
        for events in cases:
            code = (
                "import time\n"
                + "\n".join(f"print({json.dumps(e)!r}, flush=True)" for e in events)
                + "\ntime.sleep(10)"
            )
            start = time.monotonic()
            with self.assertRaises(RuntimeError):
                guard.run_codex([sys.executable, "-c", code], "input")
            self.assertLess(time.monotonic() - start, 2)

    def test_timeout_and_output_flood_are_bounded(self):
        with patch.object(guard, "TIMEOUT", 0.2), self.assertRaises(TimeoutError):
            guard.run_codex(
                [sys.executable, "-c", "import time; time.sleep(10)"], "input"
            )
        with patch.object(guard, "MAX_OUTPUT", 4096), self.assertRaises(ValueError):
            guard.run_codex(
                [sys.executable, "-c", "import sys; sys.stderr.write('x'*100000)"],
                "input",
            )

    def test_worker_input_rejected_before_model_start(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "agent_worker.py")],
            input="x" * 65537,
            text=True,
            capture_output=True,
            timeout=2,
            env={**os.environ, "MINDIE_CODEX_BIN": "/missing/not-called"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("input exceeds", result.stderr)

    def test_session_start_is_not_registered(self):
        hooks = json.loads((SCRIPTS.parent / "hooks/hooks.json").read_text())
        self.assertNotIn("SessionStart", hooks["hooks"])


if __name__ == "__main__":
    unittest.main()
