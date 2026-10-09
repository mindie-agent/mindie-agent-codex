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
import windows_process


def run_owned_hook(argv, event, *, shell, env, timeout):
    """Keep a timed-out host shell from leaking its nested PowerShell child."""
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True, shell=shell, env=env)
    if os.name == "nt":
        process = windows_process.spawn(argv, **options)
    else:
        process = subprocess.Popen(argv, start_new_session=True, **options)
    primary = None
    try:
        stdout, stderr = process.communicate(json.dumps(event), timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except BaseException as exc:
        primary = exc
        stage_path = env.get("HOOK_TEST_STAGE")
        stage = "not-started-or-no-stage-fixture"
        if stage_path:
            try:
                stage = Path(stage_path).read_text(encoding="utf-8")
            except FileNotFoundError:
                stage = "bridge-not-started"
            except OSError as stage_error:
                stage = "stage-unreadable:" + type(stage_error).__name__
        exc.add_note(f"Hook host pid={process.pid}, returncode={process.poll()}, bridge stage={stage}")
        raise
    finally:
        try:
            if os.name == "nt":
                windows_process.close_tree(process)
            else:
                import signal
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            # Reap the owned host and finish pipe readers after tree cleanup.
            process.communicate(timeout=5)
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note("Hook process cleanup also failed: " + repr(cleanup_error))
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError as close_error:
                        if primary is None:
                            raise
                        primary.add_note("Hook pipe cleanup also failed: " + repr(close_error))


class StopHookTests(unittest.TestCase):
    host_shell = None

    def run_stop(self, plugin, *, python=None, failed=False, configured=False, **env):
        event = {"session_id": "hook-regression", "last_assistant_message": "Done"}
        commands = STOP if configured else stop_hook_commands(
            [python or sys.executable, str(plugin / "scripts/bridge.py"), "stop"]
        )
        command = commands["commandWindows" if os.name == "nt" else "command"]
        argv = [self.host_shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command] if self.host_shell else command
        # This fixture verifies protocol isolation and single delivery. Its
        # extra host-shell process needs a separate startup allowance; passing
        # here does not establish native hook latency.
        self.assertNotIn("timeout", STOP)
        timeout = 15  # Test watchdog only; no product deadline.
        with tempfile.TemporaryDirectory(prefix='mindie-hook-diagnostics-') as diagnostics:
            result = run_owned_hook(
                argv, event,
                timeout=timeout,
                shell=self.host_shell is None,
                env={**os.environ, "PLUGIN_ROOT": str(plugin),
                     'MINDIE_DIAGNOSTICS_ROOT': diagnostics, **env},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        if failed:
            self.assertEqual(result.stderr.strip(),
                "MindIE Stop helper failed; capture completion is unconfirmed; no automatic retry.")
        else:
            self.assertEqual(result.stderr.strip(), "")
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
                stage = Path(root) / "bridge-stage.txt"
                (scripts / "bridge.py").write_text(
                    "import os, sys\n"
                    "from pathlib import Path\n"
                    "stage = Path(os.environ['HOOK_TEST_STAGE'])\n"
                    "stage.write_text('bridge-started', encoding='utf-8')\n"
                    "print('{\"decision\": \"block\", \"reason\": \"repeat\"}')\n"
                    "print('capture failure', file=sys.stderr)\n"
                    f"stage.write_text('bridge-exiting-{code}', encoding='utf-8')\n"
                    f"raise SystemExit({code})\n"
                )
                self.run_stop(plugin, python=sys.executable, failed=code != 0,
                              HOOK_TEST_STAGE=str(stage))
                self.assertEqual(stage.read_text(encoding="utf-8"), f"bridge-exiting-{code}")

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

    def test_configured_hook_expands_plugin_root_with_spaces(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = Path(root) / "plugin with spaces"
            scripts = plugin / "scripts"
            scripts.mkdir(parents=True)
            received = Path(root) / "received.jsonl"
            (scripts / "bridge.py").write_text(
                "import os, sys\n"
                "with open(os.environ['HOOK_TEST_RECEIVED'], 'a') as stream:\n"
                "    stream.write(sys.stdin.read() + '\\n')\n"
            )
            event = self.run_stop(plugin, configured=True,
                                  HOOK_TEST_RECEIVED=str(received))
            self.assertEqual([json.loads(line) for line in received.read_text().splitlines()], [event])

    def test_real_bridge_missing_config_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_stop(PLUGIN, failed=True, MINDIE_AGENT_CONFIG=str(Path(root) / "missing.json"))
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
