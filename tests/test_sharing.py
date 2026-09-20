"""Community sharing gate: config states, scope, transitions and hook capture.

All checks are local mechanism tests with controlled files and stub runtimes;
no model, service or network is started.
"""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import sharing
from session_gate import Sessions


class SharingFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scope = self.root / "scope"
        self.scope.mkdir()
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
        self.community = self.root / "codex.community.json"
        self.admission = self.root / "codex.admission.sqlite3"
        self.engine.write_text(
            json.dumps(
                dict(
                    root=str(self.root / "data"),
                    domain="test",
                    admission_path=str(self.admission),
                )
            )
        )
        self.config.write_text(
            json.dumps(
                dict(
                    python=sys.executable,
                    engine_config=str(self.engine),
                    community_config=str(self.community),
                    admission_path=str(self.admission),
                    runtime_scripts=str(SCRIPTS),
                )
            )
        )
        self.environment = patch.dict(
            os.environ, MINDIE_AGENT_CONFIG=str(self.config), CODEX_THREAD_ID="manual-A"
        )
        self.environment.start()
        self.sessions = Sessions()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def settings(self, **overrides):
        value = dict(
            schema="mindie-community-config/1",
            enabled=True,
            generation="g1",
            enabled_at=time.time(),
            repository="mindie-agent/knowledge",
            branch="main",
            project_roots=[str(self.scope)],
            idle_seconds=300,
            visibility="public",
        )
        value.update(overrides)
        return value

    def write_sharing(self, **overrides):
        self.community.write_text(json.dumps(self.settings(**overrides)))

    def activate(self):
        with (
            patch.dict(os.environ, CODEX_THREAD_ID="manual-A"),
            patch.object(Path, "cwd", return_value=self.scope),
        ):
            return self.sessions.activate()

    def event(self, cwd=None, **extra):
        event = dict(
            hook_event_name="Stop",
            session_id="manual-A",
            turn_id="turn-1",
            cwd=str(cwd or self.scope),
        )
        event.update(extra)
        return event

    def bridge(self, operation, event=None, timeout=5):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), operation],
            input=json.dumps(event) if event is not None else "",
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def attempts(self, session="manual-A"):
        db = sqlite3.connect(self.sessions.path)
        try:
            return db.execute(
                "SELECT count(*) FROM attempts WHERE session=?", (session,)
            ).fetchone()[0]
        finally:
            db.close()


class GateTests(SharingFixture):
    def test_missing_disabled_and_malformed_config_skip_before_any_claim(self):
        self.activate()
        states = [
            "missing",
            "disabled",
            "malformed",
            "wrong-schema",
        ]
        for state in states:
            with self.subTest(state=state):
                self.community.unlink(missing_ok=True)
                if state == "disabled":
                    self.write_sharing(enabled=False, enabled_at=None)
                elif state == "malformed":
                    self.community.write_text("{not json")
                elif state == "wrong-schema":
                    self.community.write_text(json.dumps(dict(schema="other/1")))
                result = self.bridge("stop", self.event(last_assistant_message="Done"))
                self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
                self.assertEqual(self.attempts(), 0)

    def test_scope_comes_from_the_lease_not_the_event_cwd(self):
        self.write_sharing()
        self.activate()
        outside = self.root / "outside"
        outside.mkdir()
        # A relative event cwd is rejected at envelope validation.
        result = self.bridge(
            "stop", self.event(cwd="relative/path", last_assistant_message="Done")
        )
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.attempts(), 0)
        # An absolute event cwd outside the scope does not block capture: the
        # authorized scope is the lease's activation-time project root.
        result = self.bridge(
            "stop", self.event(cwd=str(outside), last_assistant_message="Done")
        )
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.attempts(), 1)
        # A lease activated outside the authorized roots captures nothing,
        # even when the event cwd points inside an allowed directory.
        db = sqlite3.connect(self.sessions.path)
        with db:
            db.execute("UPDATE leases SET project_root=?", (str(outside),))
        db.close()
        result = self.bridge(
            "stop",
            self.event(
                turn_id="turn-2", cwd=str(self.scope), last_assistant_message="Done"
            ),
        )
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.attempts(), 1)

    def test_transcript_event_without_final_summary_is_valid(self):
        # No final summary, but a transcript location: the event is admitted
        # (forwarding is proven by the claim row; delivery itself fails open
        # because no service runs here).
        self.write_sharing()
        self.activate()
        result = self.bridge("stop", self.event(transcript_path=str(self.scope / "t.jsonl")))
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.attempts(), 1)
        # The same turn is never claimed twice, and recursion is dropped.
        self.bridge("stop", self.event(transcript_path=str(self.scope / "t.jsonl")))
        self.bridge(
            "stop",
            self.event(
                turn_id="turn-2",
                transcript_path=str(self.scope / "t.jsonl"),
                stop_hook_active=True,
            ),
        )
        self.assertEqual(self.attempts(), 1)

    def test_event_without_any_material_is_skipped(self):
        self.write_sharing()
        self.activate()
        result = self.bridge("stop", self.event())
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.attempts(), 0)

    def test_lease_without_capture_metadata_fails_closed_for_capture_only(self):
        self.write_sharing()
        # A lease lacking capture metadata: ordinary use keeps working while
        # capture is refused.
        lease = self.activate()
        db = sqlite3.connect(self.sessions.path)
        with db:
            db.execute(
                "UPDATE leases SET project_root=NULL, root_session=NULL, activated_at=NULL"
            )
        db.close()
        self.assertEqual(
            self.sessions.check("manual-A", lease["mindie_activation"])["session"],
            "manual-A",
        )
        result = self.bridge("stop", self.event(last_assistant_message="Done"))
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.attempts(), 0)

    def test_sharing_toggle_does_not_invalidate_ordinary_activation(self):
        self.write_sharing()
        lease = self.activate()
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.write_sharing(
                    enabled=enabled, enabled_at=time.time() if enabled else None
                )
                # Fingerprint covers static bindings only: the lease survives.
                self.assertEqual(
                    self.sessions.check("manual-A", lease["mindie_activation"])[
                        "session"
                    ],
                    "manual-A",
                )


class CommandTests(SharingFixture):
    def test_enable_disable_status_cycle_is_atomic_and_bounded(self):
        result = self.bridge("sharing-status")
        self.assertEqual(json.loads(result.stdout)["state"], "off")
        # Enable requires recorded settings; unconfigured fails clearly.
        result = self.bridge("sharing-enable")
        self.assertEqual(result.returncode, 1)
        self.write_sharing(enabled=False, enabled_at=None)
        started = time.monotonic()
        enabled = json.loads(self.bridge("sharing-enable").stdout)
        self.assertEqual(enabled["status"], "enabled")
        first = enabled["generation"]
        self.assertGreaterEqual(enabled["enabled_at"], started)
        settings = sharing.validate(json.loads(self.community.read_text()))
        self.assertTrue(settings["enabled"])
        self.assertEqual(self.community.stat().st_mode & 0o777, 0o600)
        # Disable keeps settings and bumps the generation; the generation
        # change itself is the core-observed cancellation signal.
        started = time.monotonic()
        disabled = json.loads(self.bridge("sharing-disable").stdout)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(disabled["status"], "disabled")
        self.assertNotEqual(disabled["generation"], first)
        self.assertIn("rereads this generation", disabled["cancel"])
        settings = sharing.validate(json.loads(self.community.read_text()))
        self.assertFalse(settings["enabled"])
        self.assertIsNone(settings["enabled_at"])
        status = json.loads(self.bridge("sharing-status").stdout)
        self.assertEqual(status["state"], "disabled")
        self.assertEqual(status["repository"], "mindie-agent/knowledge")

    def test_reenable_uses_fresh_enabled_at_without_backfill(self):
        self.write_sharing(enabled=True, enabled_at=time.time() - 10000)
        json.loads(self.bridge("sharing-disable").stdout)
        time.sleep(0.02)
        enabled = json.loads(self.bridge("sharing-enable").stdout)
        self.assertGreater(enabled["enabled_at"], time.time() - 5)
        self.assertIn("no backfill", enabled["note"])

    def test_toggle_preserves_sibling_component_extension_keys(self):
        # fork/account plus structured bot, transaction and transport settings
        # owned by the community component survive an adapter toggle verbatim.
        extensions = dict(
            fork="sample-user/knowledge-vllm-ascend",
            account="sample-user",
            bot={"account": "sample-bot", "grok_argv": ["grok"]},
            transaction_seconds=120,
            operation_limit=60,
            config_path="/private/local/path.json",
        )
        self.write_sharing(**extensions)
        json.loads(self.bridge("sharing-disable").stdout)
        after = json.loads(self.community.read_text())
        for key, value in extensions.items():
            self.assertEqual(after[key], value, key)
        self.assertFalse(after["enabled"])
        json.loads(self.bridge("sharing-enable").stdout)
        again = json.loads(self.community.read_text())
        for key, value in extensions.items():
            self.assertEqual(again[key], value, key)

    def test_malformed_status_still_reports_and_fails_closed(self):
        self.community.write_text('{"schema": "mindie-community-config/1"}')
        status = json.loads(self.bridge("sharing-status").stdout)
        self.assertEqual(status["state"], "malformed")
        self.assertIn("fail-closed", status["capture"])


if __name__ == "__main__":
    unittest.main()
