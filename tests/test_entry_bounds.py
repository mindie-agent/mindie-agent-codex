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
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "process_guard", SCRIPTS / "process_guard.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class EntryBoundsTests(unittest.TestCase):
    def test_native_usage_retains_only_nonnegative_integer_counters(self):
        event = dict(type='turn.completed', usage=dict(input_tokens=100,
                     cached_input_tokens=20, output_tokens=5, source='private-canary'))
        result = guard.run_codex([sys.executable, '-c', f'print({json.dumps(event)!r})'], 'input')
        self.assertEqual(result, dict(input_tokens=100, cached_input_tokens=20, output_tokens=5))
        event['usage'].update(input_tokens=-1, cached_input_tokens=True, output_tokens='5')
        result = guard.run_codex([sys.executable, '-c', f'print({json.dumps(event)!r})'], 'input')
        self.assertEqual(result, {})
        self.assertIsNone(guard.run_codex([sys.executable, '-c', 'pass'], 'input'))

    def test_invalid_hooks_never_invoke_runtime(self):
        event = dict(
            hook_event_name="Stop",
            session_id="s",
            turn_id="t",
            last_assistant_message="Done",
            transcript_path=str(SCRIPTS / "synthetic.jsonl"),
        )
        cases = [
            dict(event, stop_hook_active=True),
            dict(event, hook_event_name="PreToolUse"),
            dict(event, session_id="bad\ncontext"),
            [],
            None,
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "called"
            runtime_scripts = root / "runtime-scripts"
            runtime_scripts.mkdir()
            (runtime_scripts / "admission_ops.py").write_text(
                "import json\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('called')\n"
                "print(json.dumps({'ok': True, 'result': {}}))\n"
            )
            community = root / "community.json"
            community.write_text(json.dumps({
                "schema": "mindie-community-config/1",
                "enabled": True,
                "generation": "fixture",
                "enabled_at": time.time(),
                "repository": "mindie-agent/knowledge",
                "branch": "main",
                "project_roots": [str(root)],
                "idle_seconds": 300,
                "visibility": "public",
            }))
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(dict(
                    python=sys.executable,
                    engine_config=str(root / "engine.json"),
                    runtime_scripts=str(runtime_scripts),
                    community_config=str(community),
                ))
            )
            for index, case in enumerate(cases):
                if isinstance(case, dict):
                    case = dict(case, cwd=str(root))
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / "bridge.py"), "stop"],
                    input=json.dumps(case),
                    text=True,
                    capture_output=True,
                    env={**os.environ, "MINDIE_AGENT_CONFIG": str(config)},
                    timeout=3,
                )
                self.assertEqual(result.returncode, 1 if index == 2 else 0)
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
            with self.assertRaises(guard.NativeFailure):
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
        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stderr.strip(), "summary failed: invalid_result")
        self.assertNotIn("x" * 32, result.stderr)

    def test_session_start_is_not_registered(self):
        hooks = json.loads((SCRIPTS.parent / "hooks/hooks.json").read_text())
        self.assertNotIn("SessionStart", hooks["hooks"])


if __name__ == "__main__":
    unittest.main()
