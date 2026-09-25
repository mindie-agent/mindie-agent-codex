"""Launcher dispatch and temporary-network retry. No native install or network."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import auto_update  # noqa: E402
from auto_update import Updater, atomic  # noqa: E402
from bounded_process import run  # noqa: E402

SECRET = "SENTINEL_SECRET_http://user:password@evil.example/secret"
SHA = "ab" * 20


def _script(body):
    return "import sys\n" + body


NET = _script(
    f"sys.stdout.write({SECRET!r} + '\\n')\n"
    "sys.stdout.write('ERROR: Could not fetch URL\\n')\n"
    "sys.stdout.write('NewConnectionError: connection reset by peer\\n')\n"
    f"sys.stderr.write({SECRET!r} + '\\n')\n"
    "raise SystemExit(1)\n"
)
RATE = _script(
    "sys.stderr.write('The requested URL returned error: 403\\n')\n"
    "sys.stderr.write('You have exceeded a secondary rate limit\\n')\n"
    "sys.stderr.write('Retry-After: 12\\n')\n"
    f"sys.stderr.write({SECRET!r} + '\\n')\n"
    "raise SystemExit(128)\n"
)
AUTH = _script(
    f"sys.stderr.write('Authentication failed for {SECRET}\\n')\n"
    "sys.stderr.write('The requested URL returned error: 403\\n')\n"
    "raise SystemExit(128)\n"
)
PERM = _script(
    "sys.stderr.write('The requested URL returned error: 403\\n')\n"
    "raise SystemExit(128)\n"
)
MISSING = _script(
    "sys.stderr.write('The requested URL returned error: 404\\n')\n"
    "raise SystemExit(128)\n"
)
CERT = _script(
    "sys.stderr.write('SSL: CERTIFICATE_VERIFY_FAILED\\n')\n"
    f"sys.stderr.write({SECRET!r} + '\\n')\n"
    "raise SystemExit(1)\n"
)
RESOLVER = _script(
    "sys.stdout.write('ERROR: ResolutionImpossible: conflicting dependencies\\n')\n"
    f"sys.stdout.write({SECRET!r} + '\\n')\n"
    "raise SystemExit(1)\n"
)
BARE = _script("raise SystemExit(128)\n")
HANG = _script(
    "import time\n"
    f"sys.stderr.write({SECRET!r} + '\\n')\n"
    "sys.stderr.flush()\n"
    "time.sleep(30)\n"
)


def _public(exc):
    return json.dumps({
        "error": str(exc),
        "category": getattr(exc, "category", None),
        "retry_after": getattr(exc, "retry_after", None),
    })


def _run(body, *, transport, timeout=5):
    return run(
        [sys.executable, "-c", body],
        "",
        timeout=timeout,
        transport=transport,
    )


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


class ClassificationTests(unittest.TestCase):
    def assertClean(self, exc):
        blob = _public(exc)
        self.assertNotIn("SENTINEL", blob)
        self.assertNotIn("password", blob)
        self.assertNotIn("evil.example", blob)
        self.assertNotIn("http://", blob)
        self.assertNotIn(sys.executable, blob)

    def test_subprocess_classification(self):
        with self.assertRaises(RuntimeError) as caught:
            _run(NET, transport=True)
        self.assertClean(caught.exception)
        self.assertEqual(caught.exception.category, "temporary_network")
        self.assertIsInstance(caught.exception, RuntimeError)

        with self.assertRaises(RuntimeError) as caught:
            _run(RATE, transport=True)
        self.assertClean(caught.exception)
        self.assertEqual(caught.exception.category, "rate_limited")
        self.assertEqual(caught.exception.retry_after, 12.0)

        with self.assertRaises(RuntimeError) as caught:
            _run(AUTH, transport=True)
        self.assertClean(caught.exception)
        self.assertEqual(caught.exception.category, "authentication")
        self.assertNotEqual(getattr(caught.exception, "category", None), "temporary_network")

        with self.assertRaises(RuntimeError) as caught:
            _run(PERM, transport=True)
        self.assertEqual(caught.exception.category, "permission")

        with self.assertRaises(RuntimeError) as caught:
            _run(MISSING, transport=True)
        self.assertIsNone(getattr(caught.exception, "category", None))

        with self.assertRaises(RuntimeError) as caught:
            _run(BARE, transport=True)
        self.assertIsNone(getattr(caught.exception, "category", None))

        with self.assertRaises(RuntimeError) as caught:
            _run(CERT, transport=True)
        self.assertClean(caught.exception)
        self.assertEqual(caught.exception.category, "certificate")

        with self.assertRaises(RuntimeError) as caught:
            _run(RESOLVER, transport=True)
        self.assertClean(caught.exception)
        self.assertEqual(caught.exception.category, "resolver")

        with self.assertRaises(RuntimeError) as caught:
            _run(NET, transport=False)
        self.assertIsNone(getattr(caught.exception, "category", None))
        self.assertEqual(str(caught.exception), "MindIE runtime failed; not retried")

        proxy = _script(
            "sys.stderr.write(\"fatal: unable to access 'https://example.invalid/repo.git/': "
            "Proxy CONNECT aborted\\n\")\n"
            "raise SystemExit(128)\n"
        )
        with self.assertRaises(RuntimeError) as caught:
            _run(proxy, transport=True)
        self.assertClean(caught.exception)
        self.assertEqual(caught.exception.category, "temporary_network")

        pip_then_summary = _script(
            "sys.stdout.write('ERROR: NewConnectionError: connection refused\\n')\n"
            "sys.stdout.write('ERROR: Could not find a version that satisfies the requirement example\\n')\n"
            "sys.stdout.write('ERROR: No matching distribution found for example\\n')\n"
            f"sys.stdout.write({SECRET!r} + '\\n')\n"
            "raise SystemExit(1)\n"
        )
        with self.assertRaises(RuntimeError) as caught:
            _run(pip_then_summary, transport=True)
        self.assertClean(caught.exception)
        self.assertEqual(caught.exception.category, "temporary_network")

        with self.assertRaises(RuntimeError) as caught:
            _run(
                _script(
                    "sys.stdout.write('A' * 70000)\n"
                    "sys.stderr.write('fatal: failed to connect to server\\n')\n"
                    "raise SystemExit(128)\n"
                ),
                transport=True,
            )
        self.assertEqual(caught.exception.category, "temporary_network")
        self.assertNotIn("A" * 20, str(caught.exception))

        with self.assertRaises(RuntimeError) as caught:
            _run(
                _script(
                    "sys.stdout.write('ERROR: THESE PACKAGES DO NOT MATCH THE HASHES\\n')\n"
                    "raise SystemExit(1)\n"
                ),
                transport=True,
            )
        self.assertEqual(caught.exception.category, "bad_content")

    def test_transport_timeout_is_classified_without_output(self):
        with self.assertRaises(TimeoutError) as caught:
            _run(HANG, transport=True, timeout=0.4)
        self.assertClean(caught.exception)
        self.assertEqual(caught.exception.category, "temporary_network")
        self.assertIn("not retried", str(caught.exception))

        with self.assertRaises(TimeoutError) as plain:
            _run(HANG, transport=False, timeout=0.4)
        self.assertIsNone(getattr(plain.exception, "category", None))
        self.assertIn("outcome may be unknown", str(plain.exception))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "updates"
        self.root.mkdir()
        self.config = self.base / "adapter.json"
        self.config.write_text("{}\n")
        self.settings = self.base / "updater.json"
        atomic(self.settings, {
            "root": str(self.root),
            "adapter_config": str(self.config),
            "repository": str(self.base / "remote"),
            "channel": "main",
            "python": sys.executable,
            "codex": "fixture-codex",
            "uv": "fixture-uv",
            "codex_home": str(self.base / "codex"),
        })
        self.updater = Updater(self.settings)
        self.updater.check_knowledge = lambda: None
        self.updater.maintain_diagnostics = lambda: {"status": "deferred"}
        self.calls = {"prepare": 0, "install": 0, "resolve": 0}
        self.clock = Clock()
        self._time = patch("auto_update.time.time", self.clock)
        self._time.start()

    def tearDown(self):
        self._time.stop()
        self.tmp.cleanup()

    def check(self):
        self.updater.state["next_check"] = 0
        atomic(self.updater.state_path, self.updater.state)
        return self.updater.check()

    def _candidate(self):
        return {
            "revision": SHA,
            "plugin": str(self.root / "generations" / SHA / "plugin"),
            "python": sys.executable,
            "version": "0.0.0",
        }

    def test_same_sha_network_retries_past_three_then_clears(self):
        def prepare(sha):
            self.calls["prepare"] += 1
            if self.calls["prepare"] <= 4:
                _run(NET, transport=True)
            return self._candidate()

        def install(candidate):
            self.calls["install"] += 1
            return self.updater.save(
                "installed", error=None, current=candidate, candidate=sha_of(candidate)
            )

        def sha_of(candidate):
            return candidate["revision"]

        self.updater.resolve = lambda: SHA
        self.updater.prepare = prepare
        self.updater.install = install

        first = self.check()
        self.assertEqual(first["status"], "update_failed")
        self.assertEqual(first["failure_class"], "temporary_network")
        self.assertGreater(first["next_retry_at"], self.clock.now)
        self.assertNotIn("SENTINEL", json.dumps(first))
        record = first["attempts"][SHA]
        self.assertEqual(record["count"], 1)
        self.assertEqual(record["first_failure_at"], self.clock.now)
        held = self.check()
        self.assertEqual(held["status"], "update_failed")
        self.assertEqual(self.calls["prepare"], 1)
        self.assertEqual(held["attempts"][SHA]["count"], 1)

        for expected in (2, 3, 4):
            self.clock.now += 8 * 3600
            result = self.check()
            self.assertEqual(result["status"], "update_failed", expected)
            self.assertNotEqual(result["status"], "attempts_exhausted")
            self.assertEqual(result["attempts"][SHA]["count"], expected)
            self.assertEqual(self.calls["prepare"], expected)

        self.clock.now += 8 * 3600
        done = self.check()
        self.assertEqual(done["status"], "installed")
        self.assertEqual(self.calls["prepare"], 5)
        self.assertEqual(self.calls["install"], 1)
        self.assertIsNone(done.get("error"))
        self.assertNotIn("next_retry_at", done)
        self.assertNotIn("failure_class", done)
        kept = done["attempts"][SHA]
        # The successful attempt is counted, same as a crash before install returns.
        self.assertEqual(kept["count"], 5)
        self.assertIn("first_failure_at", kept)
        self.assertNotIn("next_retry_at", kept)
        self.assertNotIn("failure_class", kept)
        self.assertTrue(kept.get("history"))
        self.assertNotIn("SENTINEL", json.dumps(done))

    def test_legacy_exhaustion_stays_visible(self):
        atomic(self.updater.state_path, {
            "attempts": {SHA: {"count": 3, "last_error": "update attempt limit reached"}},
            "next_check": 0,
        })
        def prepare(sha):
            raise AssertionError("prepared")

        self.updater.resolve = lambda: SHA
        self.updater.prepare = prepare
        result = self.updater.check()
        self.assertEqual(result["status"], "attempts_exhausted")
        self.assertEqual(result["error"], "update attempt limit reached")
        self.assertNotIn("failure_class", result.get("attempts", {}).get(SHA, {}))
        self.assertNotEqual(result.get("failure_class"), "temporary_network")

    def test_certificate_is_actionable_and_retried(self):
        def prepare(sha):
            self.calls["prepare"] += 1
            _run(CERT, transport=True)

        self.updater.resolve = lambda: SHA
        self.updater.prepare = prepare
        result = self.check()
        self.assertEqual(result["status"], "action_required")
        self.assertEqual(result["failure_class"], "certificate")
        self.assertNotIn("quarantined", result.get("attempts", {}).get(SHA, {}))
        self.assertGreater(result["next_retry_at"], self.clock.now)
        self.assertNotIn("SENTINEL", json.dumps(result))
        held = self.check()
        self.assertEqual(self.calls["prepare"], 1)
        self.assertEqual(held["status"], "action_required")
        self.clock.now = result["next_retry_at"] + 1
        again = self.check()
        self.assertEqual(self.calls["prepare"], 2)
        self.assertEqual(again["failure_class"], "certificate")

    def test_release_retry_after_and_rate_limit_headers(self):
        from email.message import Message

        later = auto_update._header_retry_after({"Retry-After": "90000"})
        self.assertGreater(later, 86400)
        dated = Message()
        dated["Retry-After"] = "Wed, 21 Oct 2099 07:28:00 GMT"
        parsed = auto_update._header_retry_after(dated)
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, 86400)
        self.assertIsNone(auto_update._header_retry_after({"Retry-After": "nan"}))
        self.assertIsNone(auto_update._header_retry_after({"Retry-After": "inf"}))
        limited = auto_update._http_failure(403, {"Retry-After": "30"}, "")
        self.assertEqual(limited.category, "rate_limited")
        self.assertEqual(limited.retry_after, 30.0)
        self.assertNotIn("Retry-After", str(limited))
        remaining = auto_update._http_failure(
            403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "30"}, ""
        )
        self.assertEqual(remaining.category, "rate_limited")
        bare = auto_update._http_failure(403, {}, "forbidden")
        self.assertEqual(bare.category, "permission")
        self.assertIsNone(getattr(bare, "retry_after", None))

    def test_journal_waits_until_due_and_clean_current_refreshes_launcher(self):
        atomic(self.root / "transaction.json", {"candidate": SHA})
        self.updater.state = {"next_check": self.clock.now + 3600, "status": "installed"}
        atomic(self.updater.state_path, self.updater.state)
        calls = []

        def recover(self=None):
            calls.append("recover")
            raise RuntimeError("controlled recovery failure")

        self.updater.recover = recover
        result = self.updater._check_plugin()
        self.assertEqual(calls, [])
        self.assertGreater(result.get("next_check", 0), self.clock.now)
        (self.root / "transaction.json").unlink()
        plugin = self.root / "generations" / "local" / "plugin" / "scripts"
        plugin.mkdir(parents=True)
        (plugin / "update_launcher.py").write_bytes(b"print('new-dispatcher')\n")
        (self.root / "launcher.py").write_bytes(b"print('old-check-only')\n")
        state = {
            "next_check": self.clock.now + 3600,
            "status": "up_to_date",
            "current": {
                "plugin": str(plugin.parent),
                "revision": "local",
            },
        }
        atomic(self.updater.state_path, state)
        self.updater.state = state

        def resolve():
            raise AssertionError("resolved before due")

        self.updater.resolve = resolve
        refreshed = self.updater.check()
        self.assertEqual(calls, [])
        self.assertEqual((self.root / "launcher.py").read_bytes(), b"print('new-dispatcher')\n")
        self.assertGreater(refreshed.get("next_check", 0), self.clock.now)
        self.clock.now += 3601
        atomic(self.root / "transaction.json", {"candidate": SHA})
        self.updater.state = {
            "next_check": 0,
            "status": "up_to_date",
            "current": state["current"],
        }
        atomic(self.updater.state_path, self.updater.state)
        self.updater._check_plugin()
        self.assertEqual(calls, ["recover"])

    def test_unknown_install_timeout_stays_exhausted(self):
        def prepare(sha):
            self.calls["prepare"] += 1
            return self._candidate()

        def install(candidate):
            self.calls["install"] += 1
            raise TimeoutError(
                "MindIE request deadline exceeded; outcome may be unknown; not retried"
            )

        self.updater.resolve = lambda: SHA
        self.updater.prepare = prepare
        self.updater.install = install
        for _ in range(3):
            self.clock.now += 8 * 3600
            result = self.check()
            self.assertEqual(result["status"], "update_failed")
            record = result["attempts"][SHA]
            self.assertNotIn("next_retry_at", record)
            self.assertNotIn("failure_class", record)
        self.clock.now += 8 * 3600
        blocked = self.check()
        self.assertEqual(blocked["status"], "attempts_exhausted")
        self.assertEqual(self.calls["install"], 3)

    def test_resolve_network_waits_for_backoff(self):
        def resolve():
            self.calls["resolve"] += 1
            _run(NET, transport=True)

        self.updater.resolve = resolve
        first = self.updater.check()
        self.assertEqual(first["status"], "check_failed")
        self.assertEqual(first["failure_class"], "temporary_network")
        self.assertGreater(first["next_check"], self.clock.now)
        self.assertNotIn("SENTINEL", json.dumps(first))
        second = self.updater.check()
        self.assertEqual(self.calls["resolve"], 1)
        self.assertEqual(second["status"], "check_failed")
        self.clock.now = first["next_retry_at"] + 1
        third = self.updater.check()
        self.assertEqual(self.calls["resolve"], 2)
        self.assertEqual(third["failure_class"], "temporary_network")
        self.assertEqual(third.get("check_failures"), 2)
        self.assertEqual(third.get("check_failure_at"), 1_700_000_000.0)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "updates"
        self.plugin = self.root / "generations" / "local" / "plugin"
        scripts = self.plugin / "scripts"
        scripts.mkdir(parents=True)
        self.marker = self.base / "marker.json"
        (scripts / "auto_update.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['MARKER']).write_text(json.dumps({\n"
            "    'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n"
            "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n"
        )
        self.settings = self.base / "updater.json"
        self.settings.write_text(json.dumps({"root": str(self.root)}) + "\n")
        atomic_state = {
            "current": {"plugin": str(self.plugin), "revision": "local"},
        }
        (self.root / "state.json").write_text(json.dumps(atomic_state) + "\n")
        self.launcher = SCRIPTS / "update_launcher.py"
        self.other = self.base / "unrelated"
        self.other.mkdir()
        self.env = dict(os.environ, MARKER=str(self.marker), HOME=str(self.base / "home"))
        (self.base / "home").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self.launcher), str(self.settings), *args],
            cwd=self.other,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_dispatches_only_the_allowlist_from_another_cwd(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(
            payload["argv"],
            ["--settings", str(self.settings.resolve()), "check"],
        )
        self.assertEqual(Path(payload["cwd"]).resolve(), self.other.resolve())
        self.marker.unlink()

        proc = self._run("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["argv"][-1], "status")
        self.marker.unlink()

        proc = self._run("uninstall", "--purge")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout)["argv"][-2:],
            ["uninstall", "--purge"],
        )
        self.marker.unlink()

        for args in (
            ["enable"],
            ["status", "--purge"],
            ["disable", "--purge"],
            ["uninstall", "--force"],
            ["check", "--force"],
        ):
            self.marker.write_text("started")
            proc = self._run(*args)
            self.assertNotEqual(proc.returncode, 0, args)
            self.assertIn("unsupported launcher operation", proc.stderr)
            self.assertEqual(self.marker.read_text(), "started")

    def test_invalid_current_fails_without_running_updater(self):
        state = self.root / "state.json"
        state.write_text(json.dumps({"current": {"plugin": "/etc"}}) + "\n")
        before = state.read_bytes()
        proc = self._run("status")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid current", proc.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(state.read_bytes(), before)

    def test_real_status_has_no_side_effects(self):
        for name in (
            "auto_update.py", "bounded_process.py", "session_gate.py", "update_lock.py",
        ):
            shutil.copy(SCRIPTS / name, self.plugin / "scripts" / name)
        state = self.root / "state.json"
        before = state.read_bytes()
        names = sorted(path.name for path in self.root.iterdir())
        proc = self._run("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"]["current"]["revision"], "local")
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), names)
        self.assertFalse((self.root / "launcher.next").exists())


if __name__ == "__main__":
    unittest.main()
