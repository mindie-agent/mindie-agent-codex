"""Exercise the configured shell entrypoint, including an evicted plugin cache.

Codex treats exit code 2 from Stop as a request to continue the conversation.
Python also uses 2 for a missing script, before bridge.py can handle failures.
Summary collection must never turn an infrastructure failure into a new turn.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mindie-agent"
STOP = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]["Stop"][0][
    "hooks"
][0]


class StopHookTests(unittest.TestCase):
    def run_stop(self, plugin, **env):
        event = {"session_id": "hook-regression", "last_assistant_message": "Done"}
        result = subprocess.run(
            ["/bin/sh", "-c", STOP["command"]],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            timeout=STOP["timeout"],
            env={**os.environ, "PLUGIN_ROOT": str(plugin), **env},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(result.stderr, "")
        return event

    def test_evicted_plugin_cache_does_not_resume_conversation(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_stop(Path(root) / "missing cache")
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_missing_interpreter_does_not_resume_conversation(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_stop(PLUGIN, PATH=root)

    def test_bridge_failures_and_outputs_cannot_control_conversation(self):
        for code in (0, 1, 2, 127):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                plugin = Path(root) / "plugin with spaces"
                scripts = plugin / "scripts"
                scripts.mkdir(parents=True)
                (scripts / "bridge.py").write_text(
                    "import sys\n"
                    "print('{\"decision\": \"block\", \"reason\": \"repeat\"}')\n"
                    "print('capture failure', file=sys.stderr)\n"
                    f"raise SystemExit({code})\n"
                )
                self.run_stop(plugin)

    def test_success_still_delivers_event_once(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = Path(root) / "plugin with spaces"
            scripts = plugin / "scripts"
            scripts.mkdir(parents=True)
            received = Path(root) / "received.jsonl"
            (scripts / "bridge.py").write_text(
                "import os, sys\n"
                "assert sys.argv[1:] == ['stop']\n"
                "with open(os.environ['HOOK_TEST_RECEIVED'], 'a') as stream:\n"
                "    stream.write(sys.stdin.read() + '\\n')\n"
                "print('{}')\n"
            )
            event = self.run_stop(plugin, HOOK_TEST_RECEIVED=str(received))
            self.assertEqual(
                [json.loads(line) for line in received.read_text().splitlines()], [event]
            )

    def test_real_bridge_missing_config_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_stop(PLUGIN, MINDIE_AGENT_CONFIG=str(Path(root) / "missing.json"))
            self.assertEqual(list(Path(root).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
