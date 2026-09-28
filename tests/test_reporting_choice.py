"""Reporting choice vs the real reporter: the saved preference constrains the
service, and a saved later/disabled is never re-asked. Local files, stub
free upstream package; no reporter is started, nothing is uploaded.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tests.process_fixtures import cleanup_temporary_directory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import consent


class ReportingFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
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
                    community_config=str(self.root / "mindie-community.json"),
                    admission_path=str(self.admission),
                    runtime_scripts=str(SCRIPTS),
                )
            )
        )
        self.diag = self.root / "diag"
        self.diag.mkdir()
        self.policy = self.diag / "diagnostics.json"
        self.environment = patch.dict(
            os.environ,
            MINDIE_AGENT_CONFIG=str(self.config),
            MINDIE_DIAGNOSTICS_CONFIG=str(self.policy),
            MINDIE_DIAGNOSTICS_ROOT=str(self.diag / "logs"),
            CODEX_THREAD_ID="manual-A",
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        cleanup_temporary_directory(self.temp)

    def write_consent(self, choice="read-only", reporting="later"):
        consent.record_choice(choice)
        consent.record_reporting(reporting)

    def write_policy(self, decision):
        self.policy.write_text(
            json.dumps(
                {
                    "schema": "mindie.diagnostics.reporting.v1",
                    "purpose": "tool_fault_reporting",
                    "decision": decision,
                    "repository": "mindie-agent/mindie-agent",
                    "revision": "a" * 32,
                    "roots": [str(self.diag / "logs")],
                }
            )
            + "\n"
        )

    def bridge(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), *args],
            text=True, capture_output=True, timeout=15, cwd=str(self.root),
        )

    def status(self):
        result = self.bridge("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_saved_reporting_later_is_not_reasked(self):
        self.write_consent("read-only", "later")
        payload = self.status()
        diagnostics = payload.get("diagnostics") or {}
        self.assertIsNone(diagnostics.get("choice"))
        self.assertIsNot((diagnostics.get("reporting") or {}).get("enabled"), True)

    def test_saved_later_overrides_an_enabled_policy_on_status(self):
        self.write_consent("read-only", "later")
        self.write_policy("enabled")
        payload = self.status()
        reporting = (payload.get("diagnostics") or {}).get("reporting") or {}
        self.assertIsNot(reporting.get("enabled"), True)
        self.assertIn("saved", reporting.get("detail", ""))
        self.assertIsNone((payload.get("diagnostics") or {}).get("choice"))

    def test_reporting_ensure_requires_saved_enabled(self):
        self.write_consent("read-only", "later")
        self.write_policy("enabled")
        result = self.bridge("reporting-ensure")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["error"]["stage"], "consent")

    def test_reporting_disable_writes_policy_and_preference_together(self):
        self.write_consent("read-only", "enabled")
        self.write_policy("enabled")
        result = self.bridge("reporting-disable")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        policy = json.loads(self.policy.read_text())
        self.assertEqual(policy["decision"], "disabled")
        saved = consent.load()
        self.assertEqual(saved["reporting"], "disabled")
        # And the next status shows neither the offer nor an enabled reporter.
        payload = self.status()
        diagnostics = payload.get("diagnostics") or {}
        self.assertIsNone(diagnostics.get("choice"))
        self.assertIsNot((diagnostics.get("reporting") or {}).get("enabled"), True)

    def test_reporting_enable_records_preference_first(self):
        self.write_consent("read-only", "later")
        result = self.bridge("reporting-enable")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        saved = consent.load()
        self.assertEqual(saved["reporting"], "enabled")
        self.assertEqual(saved["choice"], "read-only")  # contribution untouched
        policy = json.loads(self.policy.read_text())
        self.assertEqual(policy["decision"], "enabled")
        payload = self.status()
        reporting = (payload.get("diagnostics") or {}).get("reporting") or {}
        self.assertIs(reporting.get("enabled"), True)

    def test_damaged_consent_refuses_preference_change_and_keeps_policy(self):
        self.write_consent("read-only", "later")
        self.write_policy("disabled")
        path = consent.consent_path()
        path.write_text("{broken")
        result = self.bridge("reporting-enable")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(path.read_text(), "{broken")
        self.assertEqual(json.loads(self.policy.read_text())["decision"], "disabled")


if __name__ == "__main__":
    unittest.main()
