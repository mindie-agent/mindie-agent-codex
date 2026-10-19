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

    def test_legacy_reporting_enable_guides_to_contribution_without_writing(self):
        consent.record_choice("disabled")
        before = consent.consent_path().read_bytes()
        result = self.bridge("reporting-enable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "contribution_choice_required")
        self.assertEqual(consent.consent_path().read_bytes(), before)
        self.assertFalse(self.policy.exists())

    def test_legacy_contribution_needs_new_explicit_choice_and_cannot_start_worker(self):
        authority = consent.consent_path()
        authority.write_text(json.dumps(dict(schema=consent.SCHEMA, choice="contribute", reporting="enabled")))
        before = authority.read_bytes()
        result = self.bridge("reporting-ensure")
        self.assertEqual(result.returncode, 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "needs_choice")
        self.assertFalse(value["enabled"])
        self.assertEqual(value["worker"]["status"], "not_started")
        self.assertEqual(authority.read_bytes(), before)

    def test_explicit_choice_activates_the_worker_without_rewriting_choice(self):
        import diagnostic_support
        consent.record_choice("contribute")
        before = consent.consent_path().read_bytes()
        with patch("mindie_diagnostics.reporting_commands.ensure", return_value={"status": "running"}) as start:
            result = diagnostic_support.activate_reporting()
        self.assertEqual(result["status"], "running")
        self.assertEqual(Path(start.call_args.args[0]), self.policy)
        self.assertEqual(consent.consent_path().read_bytes(), before)
        policy = json.loads(self.policy.read_text())
        self.assertEqual(policy["consent_config"], str(consent.consent_path()))

    def test_disabled_choice_wins_over_previous_enabled_policy(self):
        import diagnostic_support
        consent.record_choice("contribute")
        diagnostic_support.activate_reporting(ensure=False)
        consent.record_choice("disabled")
        result = self.bridge("reporting-status")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value["enabled"])

    def bridge(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), *args],
            text=True, capture_output=True, timeout=15, cwd=str(self.root),
        )
