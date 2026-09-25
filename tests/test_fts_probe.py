"""Behavioral FTS probe checks. The unsupported double is not an old or Windows SQLite.

API contract assertions stay in test_runtime_compatibility.py.
"""

import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import auto_update
from bounded_process import run
import setup as setup_script

CAPABILITY = "FTS5 contentless_delete=1 (SQLite >=3.43.0)"
FAULT = "print('OK', flush=True)\nraise RuntimeError('controlled prior API failure')\n"
SQLITE_PATCH = r"""
class _Bad:
    def execute(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("no such module: fts5")
    def close(self):
        open(__import__("os").environ["FTS_CLOSE_PATH"], "w").write("closed")
def _connect(name, *_args, **_kwargs):
    if name != ":memory:":
        raise AssertionError(name)
    return _Bad()
sqlite3.connect = _connect
"""


def _exec(source, connect, namespace=None):
    real = sqlite3.connect
    sqlite3.connect = connect
    namespace = {} if namespace is None else namespace
    try:
        exec(source, namespace)
    finally:
        sqlite3.connect = real
    return namespace


class _Recording:
    def __init__(self, raw, name, closed):
        self._raw = raw
        self._name = name
        self._closed = closed

    def execute(self, *args, **kwargs):
        return self._raw.execute(*args, **kwargs)

    def close(self):
        self._closed.append(self._name)
        return self._raw.close()


def _recording_connect(seen, closed):
    real = sqlite3.connect

    def connect(name, *args, **kwargs):
        seen.append(name)
        return _Recording(real(name, *args, **kwargs), name, closed)

    return connect


class _Unsupported:
    def __init__(self):
        self.closed = False

    def execute(self, _sql, *_args):
        raise sqlite3.OperationalError("no such module: fts5")

    def close(self):
        self.closed = True


def _child_env(extra=None):
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


class SetupFtsProbeTests(unittest.TestCase):
    def test_supported_ddl_succeeds_and_closes(self):
        seen, closed = [], []
        namespace = _exec(
            setup_script._FTS_PROBE,
            _recording_connect(seen, closed),
            {"missing": ["import-already-failed"]},
        )
        self.assertEqual(namespace["missing"], ["import-already-failed"])
        self.assertEqual(seen, [":memory:"])
        self.assertEqual(closed, [":memory:"])
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, "-c", setup_script._FTS_PROBE],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=15,
                env=_child_env(),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_unsupported_error_names_actual_version_and_closes(self):
        fake = _Unsupported()

        def connect(name, *args, **kwargs):
            self.assertEqual(name, ":memory:")
            return fake

        namespace = {"missing": ["x" * 500]}
        _exec(setup_script._FTS_PROBE, connect, namespace)
        self.assertTrue(fake.closed)
        text = namespace["missing"][0]
        self.assertTrue(text.startswith("sqlite " + sqlite3.sqlite_version + " lacks "))
        self.assertIn(CAPABILITY, text[:200])
        self.assertIn(sqlite3.sqlite_version, text[:200])
        self.assertIn("OperationalError", text)
        shown = ("MISSING: " + "; ".join(namespace["missing"])).strip()[:300]
        self.assertIn(sqlite3.sqlite_version, shown)
        self.assertIn(CAPABILITY, shown)


class UpdaterFtsProbeTests(unittest.TestCase):
    def test_supported_ddl_succeeds_and_closes(self):
        seen, closed = [], []
        buf = io.StringIO()
        with redirect_stdout(buf):
            _exec(auto_update._FTS_PROBE, _recording_connect(seen, closed))
        self.assertEqual(buf.getvalue().strip(), "OK")
        self.assertEqual(seen, [":memory:"])
        self.assertEqual(closed, [":memory:"])
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, "-c", auto_update._FTS_PROBE],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=15,
                env=_child_env(),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "OK")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_unsupported_capability_exits_zero_and_is_rejected(self):
        fake = _Unsupported()

        def connect(name, *args, **kwargs):
            self.assertEqual(name, ":memory:")
            return fake

        buf = io.StringIO()
        with redirect_stdout(buf):
            _exec(auto_update._FTS_PROBE, connect)
        self.assertTrue(fake.closed)
        printed = buf.getvalue().strip()
        self.assertTrue(printed.startswith("MISSING: sqlite " + sqlite3.sqlite_version + " lacks "))
        self.assertIn(CAPABILITY, printed[:200])
        self.assertNotIn("OK", printed.splitlines())

        continued = []

        def command(argv, **kwargs):
            script = str(argv[2])
            at = script.rindex("import sqlite3\n")
            child = script[at:]
            child = child.replace("import sqlite3\n", "import sqlite3\n" + SQLITE_PATCH, 1)
            return run([str(argv[0]), "-c", child], "", **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FTS_CLOSE_PATH"] = str(Path(tmp) / "closed")
            try:
                with self.assertRaises(RuntimeError) as caught:
                    auto_update.Updater.probe_runtime(
                        SimpleNamespace(command=command), sys.executable
                    )
                    continued.append("install")
            finally:
                os.environ.pop("FTS_CLOSE_PATH", None)
            self.assertEqual((Path(tmp) / "closed").read_text(), "closed")
        self.assertEqual(continued, [])
        text = str(caught.exception)
        self.assertIn(sqlite3.sqlite_version, text[:200])
        self.assertIn(CAPABILITY, text[:200])
        self.assertNotEqual(text, "MindIE runtime failed; not retried")
        self.assertNotIn("Traceback", text)

    def test_exit1_after_ok_is_rejected_before_install(self):
        direct = subprocess.run(
            [sys.executable, "-c", FAULT],
            capture_output=True,
            text=True,
            timeout=15,
            env=_child_env(),
        )
        self.assertEqual(direct.returncode, 1)
        self.assertEqual(direct.stdout, "OK\n")
        with self.assertRaises(RuntimeError) as strict:
            run([sys.executable, "-c", FAULT], "", timeout=15)
        self.assertEqual(str(strict.exception), "MindIE runtime failed; not retried")

        continued = []

        def command(argv, **kwargs):
            argv = [str(arg) for arg in argv]
            argv[2] = FAULT + argv[2]
            return run(argv, "", **kwargs)

        with self.assertRaises(RuntimeError) as caught:
            auto_update.Updater.probe_runtime(
                SimpleNamespace(command=command), sys.executable
            )
            continued.append("install")
        self.assertEqual(continued, [])
        self.assertEqual(str(caught.exception), "MindIE runtime failed; not retried")


if __name__ == "__main__":
    unittest.main()
