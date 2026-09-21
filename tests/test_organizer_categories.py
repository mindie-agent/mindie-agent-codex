"""Typed organizer failure categories; local children only, no model calls."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"

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


ORGANIZE = {
    "role": "organize",
    "domain": "vllm-ascend",
    "increment": "local probe",
    "coverage": {},
    "existing_drafts": [],
}


def _worker_cli(payload, *, binary=None, extra_env=None, raw=None, timeout=5):
    env = {**os.environ, "MINDIE_CODEX_BIN": binary or "/missing/codex-not-called"}
    if extra_env:
        env.update(extra_env)
    data = raw if raw is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "agent_worker.py")],
        input=data,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def _stub_bin(directory, source):
    path = Path(directory) / "codex-stub"
    path.write_text("#!/usr/bin/env python3\n" + source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


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
        self.assertEqual(bad.stderr.strip(), "organizer result was invalid")
        for raw in ("[]", "x" * 65537):
            invalid = _worker_cli(None, raw=raw)
            self.assertEqual(invalid.returncode, 65)
            self.assertEqual(invalid.stderr.strip(), "organizer result was invalid")
        missing = _worker_cli(ORGANIZE)
        self.assertEqual(missing.returncode, 78)
        self.assertEqual(missing.stderr.strip(), "organizer configuration failed")
        self.assertNotIn("No such file", missing.stderr)

    def test_cli_native_error_event(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = _stub_bin(
                directory,
                "import json,sys,time\n"
                "print(json.dumps({'type':'error'}), flush=True)\n"
                "time.sleep(10)\n",
            )
            result = _worker_cli(ORGANIZE, binary=binary)
        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stderr.strip(), "organizer native invocation failed")
        self.assertNotIn("error", result.stdout)

    def test_cli_malformed_result_file(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = _stub_bin(
                directory,
                "import sys\n"
                "out=sys.argv[sys.argv.index('--output-last-message')+1]\n"
                "open(out,'w').write('not-json')\n",
            )
            result = _worker_cli(ORGANIZE, binary=binary)
        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stderr.strip(), "organizer result was invalid")
        self.assertNotIn("not-json", result.stderr)

    def test_cli_result_file_output_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = _stub_bin(
                directory,
                "import sys\n"
                "out=sys.argv[sys.argv.index('--output-last-message')+1]\n"
                "open(out,'w').write('x'*40000)\n",
            )
            result = _worker_cli(ORGANIZE, binary=binary)
        self.assertEqual(result.returncode, 75)
        self.assertEqual(result.stderr.strip(), "organizer output exceeded the bound")
        self.assertNotIn("xxxx", result.stderr)

    def test_unexpected_failure_is_generic(self):
        loaded = _load_worker()
        self.assertEqual(loaded.EXIT_UNKNOWN, 2)
        self.assertFalse(isinstance(RuntimeError("secret-token"), loaded.ConfigurationError))
        driver = (
            "import runpy,sys\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import process_guard\n"
            "process_guard.run_codex = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('secret-token'))\n"
            "sys.argv = ['agent_worker.py']\n"
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
        self.assertEqual(result.stderr.strip(), "organizer failed unexpectedly")
        self.assertNotIn("secret-token", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_guard_invalid_item_shape(self):
        with self.assertRaises(guard.InvalidResultError):
            guard.run_codex([sys.executable, "-c", "print('{\"type\":\"item.started\",\"item\":[]}')"], "input")

    @unittest.skipUnless(os.name == "posix", "requires POSIX exec format errors")
    def test_unlaunchable_format_is_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-an-executable-format"
            path.write_text("PRIVATE_INVALID_BINARY_MARKER")
            path.chmod(0o700)
            result = _worker_cli(ORGANIZE, binary=str(path))
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stderr.strip(), "organizer configuration failed")
        self.assertNotIn("PRIVATE_INVALID_BINARY_MARKER", result.stderr)


if __name__ == "__main__":
    unittest.main()
