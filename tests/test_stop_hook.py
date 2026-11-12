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
import native_entry
from native_entry import neutral_hook_commands as stop_hook_commands
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
            except TimeoutError:
                raise
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
                    except TimeoutError:
                        raise
                    except OSError as close_error:
                        if primary is None:
                            raise
                        primary.add_note("Hook pipe cleanup also failed: " + repr(close_error))


class StopHookTests(unittest.TestCase):
    host_shell = None

    def run_shell(self, commands, *, env=None, timeout=10):
        """Dispatch the host-native wrapper: POSIX sh, cmd.exe EncodedCommand, or pwsh."""
        env = {**os.environ, **(env or {})}
        if os.name == "nt":
            command = commands["commandWindows"]
            if self.host_shell:
                encoded = command.split("-EncodedCommand ", 1)[1]
                argv = [self.host_shell, "-NoLogo", "-NoProfile", "-NonInteractive",
                        "-EncodedCommand", encoded]
                return subprocess.run(argv, capture_output=True, text=True,
                                      timeout=timeout, env=env)
            return subprocess.run(command, shell=True, capture_output=True, text=True,
                                  timeout=timeout, env=env)
        return subprocess.run(commands["command"], shell=True, capture_output=True,
                              text=True, timeout=timeout, env=env)

    def run_stop(self, plugin, *, python=None, failed=False, configured=False, commands=None, **env):
        event = {"session_id": "hook-regression", "last_assistant_message": "Done"}
        commands = commands or (STOP if configured else stop_hook_commands(
            [python or sys.executable, str(plugin / "scripts/bridge.py"), "stop"]
        ))
        command = commands["commandWindows" if os.name == "nt" else "command"]
        argv = [self.host_shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command] if self.host_shell else command
        # This fixture verifies protocol isolation and single delivery. Its
        # extra host-shell process needs a separate startup allowance; passing
        # here does not establish native hook latency.
        self.assertEqual(STOP["timeout"], native_entry.STOP_TIMEOUT)
        timeout = 15  # Test watchdog only; product deadline is on the host hook.
        with tempfile.TemporaryDirectory(prefix='mindie-hook-diagnostics-') as diagnostics:
            result = run_owned_hook(
                argv, event,
                timeout=timeout,
                shell=self.host_shell is None,
                env={**os.environ, "PLUGIN_ROOT": str(plugin),
                     'MINDIE_DIAGNOSTICS_ROOT': diagnostics,
                     'MINDIE_DIAGNOSTICS_CONFIG': str(Path(diagnostics) / 'diagnostics.json'), **env},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        if failed:
            message = payload["systemMessage"]
            self.assertIn(
                "MindIE Stop helper failed; capture completion is unconfirmed; no automatic retry.",
                message)
            self.assertRegex(message, r"\(exit -?\d+\)")
        else:
            self.assertEqual(payload, {})
            self.assertEqual(result.stderr.strip(), "")
        self.last_result = result
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
                self.run_stop(plugin, python=sys.executable, failed=True,
                              HOOK_TEST_STAGE=str(stage))
                self.assertEqual(stage.read_text(encoding="utf-8"), f"bridge-exiting-{code}")
                self.assertIn("capture failure", self.last_result.stderr)
                self.assertIn("capture failure", json.loads(self.last_result.stdout)["systemMessage"])

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

    def test_configured_hook_reuses_fixed_data_entry_with_spaces(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = Path(root) / "plugin with spaces"
            data = Path(root) / "plugin data with spaces"
            scripts = data / "releases/0.0.1"
            scripts.mkdir(parents=True)
            received = Path(root) / "received.jsonl"
            (scripts / "entry.py").write_text(
                "import os, sys\n"
                "with open(os.environ['HOOK_TEST_RECEIVED'], 'a') as stream:\n"
                "    stream.write(sys.stdin.read() + '\\n')\n"
            )
            validation = dict(schema="mindie-candidate-check/1", status="validated",
                              native_version="0.0.1")
            config = Path(root) / "adapter.json"
            config.write_text(json.dumps(dict(python=sys.executable, product_validation=validation)))
            (scripts / "runtime.json").write_text(json.dumps(
                dict(python=sys.executable, validation=validation)))
            self.assertIn("hook_launcher.py", STOP["command"])
            self.assertIn("$PLUGIN_DATA/hook_launcher.py", STOP["command"])
            self.assertNotIn("${PLUGIN_ROOT}", STOP["command"])
            self.assertNotIn("${PLUGIN_DATA}", STOP["command"])
            self.assertEqual(STOP["timeout"], native_entry.STOP_TIMEOUT)
            commands = stop_hook_commands(
                [sys.executable, str(PLUGIN / "scripts/hook_launcher.py"), "stop"])
            event = self.run_stop(plugin, commands=commands, PLUGIN_DATA=str(data),
                                  HOOK_TEST_RECEIVED=str(received), MINDIE_AGENT_CONFIG=str(config),
                                  PLUGIN_ROOT=str(PLUGIN))
            self.assertEqual([json.loads(line) for line in received.read_text().splitlines()], [event])

    def test_evicted_plugin_cache_uses_durable_launcher(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            missing = root / "missing cache"
            data = root / "plugin data"
            data.mkdir()
            shutil.copy2(PLUGIN / "scripts/hook_launcher.py", data / "hook_launcher.py")
            release = data / "releases/0.0.1"
            release.mkdir(parents=True)
            received = root / "received.jsonl"
            (release / "entry.py").write_text(
                "import os, sys\n"
                "with open(os.environ['HOOK_TEST_RECEIVED'], 'a') as stream:\n"
                "    stream.write(sys.stdin.read() + '\\n')\n"
            )
            validation = dict(schema="mindie-candidate-check/1", status="validated",
                              native_version="0.0.1")
            config = root / "adapter.json"
            config.write_text(json.dumps(dict(python=sys.executable, product_validation=validation)))
            (release / "runtime.json").write_text(json.dumps(
                dict(python=sys.executable, validation=validation)))
            commands = stop_hook_commands([sys.executable, "$_hl", "stop"])
            self.assertIn("$PLUGIN_DATA/hook_launcher.py", commands["command"])
            self.assertNotIn("${PLUGIN_ROOT}", commands["command"])
            event = self.run_stop(
                missing, commands=commands, PLUGIN_DATA=str(data),
                HOOK_TEST_RECEIVED=str(received), MINDIE_AGENT_CONFIG=str(config))
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

    def test_stop_wrapper_keeps_exit_zero_and_helper_stderr(self):
        commands = stop_hook_commands(
            [sys.executable, "-c", "import sys; print('boom-detail', file=sys.stderr); raise SystemExit(7)"])
        result = self.run_shell(commands)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("boom-detail", result.stderr)
        message = json.loads(result.stdout)["systemMessage"]
        self.assertIn("boom-detail", message)
        self.assertIn("(exit 7)", message)
        self.assertIn("MindIE Stop helper failed", message)

    def test_stop_exit_zero_with_stderr_emits_system_message(self):
        commands = stop_hook_commands(
            [sys.executable, "-c",
             "import sys; print('silent-fail', file=sys.stderr); raise SystemExit(0)"])
        result = self.run_shell(commands)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("silent-fail", result.stderr)
        message = json.loads(result.stdout)["systemMessage"]
        self.assertIn("silent-fail", message)
        self.assertIn("(exit 0)", message)

    def test_localized_helper_failure_remains_valid_protocol_json(self):
        commands = stop_hook_commands([
            sys.executable, "-c",
            "import sys; print('\\u9519\\u8bef\\u8be6\\u60c5', file=sys.stderr); raise SystemExit(7)",
        ])
        result = self.run_shell(commands)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\u9519\u8bef\u8be6\u60c5", json.loads(result.stdout)["systemMessage"])

    def test_session_start_wrapper_preserves_helper_exit_and_stderr(self):
        commands = stop_hook_commands(
            [sys.executable, "-c", "import sys; print('prep-detail', file=sys.stderr); raise SystemExit(7)"],
            keep_stderr=True, pass_exit=True,
            failure="MindIE session preparation failed; inspect the reported stage and local diagnostics; no automatic retry.")
        result = self.run_shell(commands)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("prep-detail", result.stderr)
        self.assertIn("(exit 7)", json.loads(result.stdout)["systemMessage"])


@unittest.skipUnless(os.name == 'nt', 'native PowerShell hook dispatch')
class PowerShellStopHookTests(StopHookTests):
    # The native Windows host uses PowerShell, while shell=True uses CMD.
    # Exercise both dispatchers with the real stdin and missing-child cases.
    host_shell = shutil.which('pwsh') or shutil.which('powershell')


if __name__ == "__main__":
    unittest.main()
