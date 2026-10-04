"""Exercise the configured shell entrypoint, including an evicted plugin cache.

Codex treats exit code 2 from Stop as a request to continue the conversation.
Python also uses 2 for a missing script, before bridge.py can handle failures.
Summary collection must never turn an infrastructure failure into a new turn.
"""

import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mindie-agent"
STOP = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]["Stop"][0][
    "hooks"
][0]
sys.path.insert(0, str(ROOT / "plugins/mindie-agent/scripts"))
from auto_update import stop_hook_commands


class StopHookTests(unittest.TestCase):
    host_shell = None

    def run_stop(self, plugin, *, python=None, failed=False, **env):
        event = {"session_id": "hook-regression", "last_assistant_message": "Done"}
        command = stop_hook_commands(
            [python or sys.executable, str(plugin / "scripts/bridge.py"), "stop"]
        )["commandWindows" if os.name == "nt" else "command"]
        argv = [self.host_shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command] if self.host_shell else command
        result = subprocess.run(
            argv,
            input=json.dumps(event),
            text=True,
            capture_output=True,
            timeout=STOP["timeout"],
            shell=self.host_shell is None,
            env={**os.environ, "PLUGIN_ROOT": str(plugin), **env},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(result.stderr.strip(),
                         "MindIE Stop capture failed before completion; inspect MindIE status." if failed else "")
        return event

    def test_evicted_plugin_cache_does_not_resume_conversation(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_stop(Path(root) / "missing cache", failed=True)
            self.assertEqual(list(Path(root).iterdir()), [])
    def test_missing_interpreter_does_not_resume_conversation(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_stop(
                PLUGIN,
                python=str(Path(root) / "missing-python.exe"), failed=True,
            )

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
                self.run_stop(plugin, python=sys.executable, failed=code != 0)

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
            event = self.run_stop(
                plugin, python=sys.executable, HOOK_TEST_RECEIVED=str(received)
            )
            self.assertEqual(
                [json.loads(line) for line in received.read_text().splitlines()], [event]
            )

    def test_real_bridge_missing_config_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_stop(PLUGIN, MINDIE_AGENT_CONFIG=str(Path(root) / "missing.json"))
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_real_bridge_corrupt_config_warns_without_resuming(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'adapter.json'
            config.write_text('{broken')
            self.run_stop(PLUGIN, failed=True, MINDIE_AGENT_CONFIG=str(config),
                          MINDIE_DIAGNOSTICS_ROOT=str(Path(root) / 'diagnostics'))
            self.assertEqual(config.read_text(), '{broken')


@unittest.skipUnless(os.name == 'nt', 'native PowerShell hook dispatch')
class PowerShellStopHookTests(StopHookTests):
    # The native Windows host uses PowerShell, while shell=True uses CMD.
    # Exercise both dispatchers with the real stdin and missing-child cases.
    host_shell = shutil.which('pwsh') or shutil.which('powershell')


if __name__ == "__main__":
    unittest.main()
