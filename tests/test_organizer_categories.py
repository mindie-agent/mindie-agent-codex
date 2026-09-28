"""Typed organizer failure categories; local children only, no model calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

guard_spec = importlib.util.spec_from_file_location(
    "process_guard", SCRIPTS / "process_guard.py"
)
guard = importlib.util.module_from_spec(guard_spec)
guard_spec.loader.exec_module(guard)

worker_spec = importlib.util.spec_from_file_location(
    "agent_worker", SCRIPTS / "agent_worker.py"
)
worker = importlib.util.module_from_spec(worker_spec)


def _load_worker():
    with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
        worker_spec.loader.exec_module(worker)
    return worker


ORGANIZE = {"role": "summarize", "text": "local synthetic probe", "partial": False}


def _worker_cli(payload, *, binary=None, extra_env=None, raw=None, timeout=5):
    env = {
        **os.environ,
        "MINDIE_CODEX_BIN": binary or "/missing/codex-not-called",
    }
    if extra_env:
        env.update(extra_env)
    data = raw if raw is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "agent_worker.py"), "--model", "synthetic-summary-model"],
        input=data,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def _worker_cli_with_invoker(payload, behavior, *, timeout=5):
    """Run the actual worker CLI with its native invoker replaced by Python code.

    The Codex binary is a native executable contract. These cases test worker
    result classification, so a Python subprocess drives the real worker CLI
    and substitutes only the native invocation boundary.
    """
    driver = (
        "import json,runpy,sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import process_guard\n"
        "behavior, worker_path = sys.argv[1], sys.argv[2]\n"
        "def invoke(command, prompt, **kwargs):\n"
        " if behavior == 'native-error':\n"
        "  raise process_guard.NativeFailure('private fixture failure')\n"
        " output = command[command.index('--output-last-message') + 1]\n"
        " if behavior == 'malformed':\n"
        "  Path(output).write_text('not-json', encoding='utf-8')\n"
        " elif behavior == 'over-limit':\n"
        "  Path(output).write_text('x' * 40000, encoding='utf-8')\n"
        " else:\n"
        "  raise AssertionError('unknown fixture behavior')\n"
        "process_guard.run_codex = invoke\n"
        "sys.argv = [worker_path, '--model', 'synthetic-summary-model']\n"
        "runpy.run_path(worker_path, run_name='__main__')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", driver, behavior, str(SCRIPTS / "agent_worker.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=dict(os.environ, MINDIE_CODEX_BIN="codex-fixture"),
    )


class OrganizerCategoryTests(unittest.TestCase):
    def test_guard_invalid_event_is_invalid_result(self):
        with self.assertRaises(guard.InvalidResultError):
            guard.run_codex(
                [sys.executable, "-c", "print('not-json', flush=True)"],
                "input",
            )

    def test_guard_nonzero_exit_is_native(self):
        with self.assertRaises(guard.NativeFailure):
            guard.run_codex(
                [sys.executable, "-c", "raise SystemExit(3)"],
                "input",
            )

    def test_wait_timeout_is_deadline(self):
        with patch.object(guard, "TIMEOUT", 0.15):
            with self.assertRaises(TimeoutError):
                guard.run_codex(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    "input",
                )

    def test_in_process_role_and_missing_bin_are_configuration(self):
        loaded = _load_worker()
        with self.assertRaises(loaded.ConfigurationError):
            loaded.run({"role": "judge", "outcome": "x"})
        with self.assertRaises(loaded.ConfigurationError):
            loaded.run({"increment": "x"})
        with self.assertRaises(loaded.ConfigurationError):
            loaded.run({"role": []})
        with patch.dict(os.environ, {"MINDIE_CODEX_BIN": "/no/such/codex"}):
            with self.assertRaises(loaded.ConfigurationError):
                loaded.run(dict(ORGANIZE))

    def test_cli_malformed_input_and_missing_bin(self):
        bad = _worker_cli(None, raw="{")
        self.assertEqual(bad.returncode, 65)
        self.assertEqual(bad.stderr.strip(), "summary failed: invalid_result")
        for raw in ("[]", "x" * 65537):
            invalid = _worker_cli(None, raw=raw)
            self.assertEqual(invalid.returncode, 65)
            self.assertEqual(invalid.stderr.strip(), "summary failed: invalid_result")
        missing = _worker_cli(ORGANIZE)
        self.assertEqual(missing.returncode, 78)
        self.assertEqual(missing.stderr.strip(), "summary failed: configuration")
        self.assertNotIn("No such file", missing.stderr)

    def test_cli_native_error_event(self):
        result = _worker_cli_with_invoker(ORGANIZE, "native-error")
        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stderr.strip(), "summary failed: native")
        self.assertNotIn("error", result.stdout)

    def test_cli_malformed_result_file(self):
        result = _worker_cli_with_invoker(ORGANIZE, "malformed")
        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stderr.strip(), "summary failed: invalid_result")
        self.assertNotIn("not-json", result.stderr)

    def test_cli_result_file_output_limit(self):
        result = _worker_cli_with_invoker(ORGANIZE, "over-limit")
        self.assertEqual(result.returncode, 75)
        self.assertEqual(result.stderr.strip(), "summary failed: output_limit")
        self.assertNotIn("xxxx", result.stderr)

    def test_unexpected_failure_is_generic(self):
        loaded = _load_worker()
        self.assertTrue(callable(loaded.main))
        self.assertFalse(isinstance(RuntimeError("secret-token"), loaded.ConfigurationError))
        driver = (
            "import runpy,sys\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import process_guard\n"
            "process_guard.run_codex = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('secret-token'))\n"
            "sys.argv = ['agent_worker.py', '--model', 'synthetic-summary-model']\n"
            f"runpy.run_path({str(SCRIPTS / 'agent_worker.py')!r}, run_name='__main__')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", driver],
            input=json.dumps(ORGANIZE),
            text=True,
            capture_output=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "summary failed: unknown")
        self.assertNotIn("secret-token", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_guard_invalid_item_shape(self):
        with self.assertRaises(guard.InvalidResultError):
            guard.run_codex([sys.executable, "-c", "print('{\"type\":\"item.started\",\"item\":[]}')"], "input")

    def test_unlaunchable_format_is_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-an-executable-format.exe"
            path.write_text("PRIVATE_INVALID_BINARY_MARKER")
            result = _worker_cli(ORGANIZE, binary=str(path))
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stderr.strip(), "summary failed: configuration")
        self.assertNotIn("PRIVATE_INVALID_BINARY_MARKER", result.stderr)


if __name__ == "__main__":
    unittest.main()
