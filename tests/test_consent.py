"""Explicit profile consent and damaged-state preservation; no inferred choices."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.process_fixtures import cleanup_temporary_directory, extract_git_archive

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import consent
import sharing


class ConsentFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
        self.engine.write_text(
            json.dumps(dict(root=str(self.root / "data"), domain="test",
                            admission_path=str(self.root / "a.sqlite3")))
        )
        self.legacy_community = self.root / "mindie-community.json"
        self.config.write_text(
            json.dumps(dict(
                python=sys.executable,
                engine_config=str(self.engine),
                community_config=str(self.legacy_community),
                admission_path=str(self.root / "a.sqlite3"),
                runtime_scripts=str(SCRIPTS),
            ))
        )
        self.environment = patch.dict(
            os.environ, MINDIE_AGENT_CONFIG=str(self.config)
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        cleanup_temporary_directory(self.temp)




    def test_record_choice_refuses_to_clear_damaged_consent(self):
        consent.consent_path().write_text("{broken")
        with self.assertRaises(consent.ConsentError) as ctx:
            consent.record_choice("later")
        self.assertEqual(ctx.exception.state, "corrupt")
        self.assertEqual(consent.consent_path().read_text(), "{broken")
        self.assertEqual(consent.load()["state"], "corrupt")

    def test_unsupported_pointer_cannot_select_an_alternate_authority(self):
        shared = consent.shared_community_path()
        shared.write_text("{broken")
        alternate = self.root / "old-community.json"
        alternate.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=True,
                 generation="g", enabled_at=1.0, repository="owner/repo",
                 project_roots=[str(self.root)], idle_seconds=300)
        ))
        config = json.loads(self.config.read_text())
        config["community_config"] = str(alternate)
        self.config.write_text(json.dumps(config))
        with self.assertRaises(ValueError):
            sharing.read()
        lease = dict(project_root=str(self.root), root_session="t",
                     activated_at=1.0)
        with self.assertRaises(ValueError):
            sharing.capture_allowed(lease, str(self.root))
        self.assertEqual(shared.read_text(), "{broken")

    def test_consent_config_gates_the_capture_write_path(self):
        # Settings carrying the consent authority: capture requires a saved
        # contribute choice; every other consent state stops the write path
        # at the adapter boundary, before any capture row or model work.
        authority = consent.consent_path()
        settings = dict(
            schema="mindie-community-config/1", enabled=True, generation="g",
            enabled_at=1.0, repository="owner/repo", branch="main",
            project_roots=[str(self.root)], idle_seconds=300,
            consent_config=str(authority),
        )
        shared = consent.shared_community_path()
        shared.write_text(json.dumps(settings))
        lease = dict(project_root=str(self.root), root_session="t",
                     activated_at=1.0)
        consent.record_choice("contribute")
        self.assertTrue(sharing.capture_allowed(lease, str(self.root)))
        for blocked in ("read-only", "later", "disabled"):
            consent.record_choice(blocked)
            self.assertFalse(sharing.capture_allowed(lease, str(self.root)), blocked)
        authority.unlink()
        with self.assertRaisesRegex(sharing.SharingError, 'missing'):
            sharing.capture_allowed(lease, str(self.root))
        authority.write_text("{broken")
        with self.assertRaisesRegex(sharing.SharingError, 'corrupt'):
            sharing.capture_allowed(lease, str(self.root))
        # The field grants nothing by itself: enabled=false still wins.
        authority.unlink()
        consent.record_choice("contribute")
        settings["enabled"] = False
        settings["enabled_at"] = None
        shared.write_text(json.dumps(settings))
        self.assertFalse(sharing.capture_allowed(lease, str(self.root)))
        # A pre-release enabled flag is not a substitute for explicit consent.
        settings["enabled"] = True
        settings["enabled_at"] = 1.0
        del settings["consent_config"]
        shared.write_text(json.dumps(settings))
        authority.unlink()
        with self.assertRaisesRegex(sharing.SharingError, "consent authority"):
            sharing.capture_allowed(lease, str(self.root))

    def test_concurrent_field_updates_both_survive(self):
        consent.record_choice("later")
        consent.record_reporting("disabled")
        script = self.root / "race_update.py"
        script.write_text(
            "import os, sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "os.environ['MINDIE_AGENT_CONFIG'] = sys.argv[2]\n"
            "from mindie_knowledge import consent_store\n"
            "real = consent_store._read_raw\n"
            "def slow(path):\n"
            "    found = real(path)\n"
            "    time.sleep(0.15)\n"
            "    return found\n"
            "consent_store._read_raw = slow\n"
            "import consent\n"
            "if sys.argv[3] == 'choice':\n"
            "    consent.record_choice('contribute')\n"
            "else:\n"
            "    consent.record_reporting('enabled')\n"
        )
        children = [
            subprocess.Popen(
                [sys.executable, str(script), str(SCRIPTS), str(self.config), op],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for op in ("choice", "reporting")
        ]
        finished = [child.communicate(timeout=15) for child in children]
        codes = [child.returncode for child in children]
        self.assertEqual(codes, [0, 0], finished)
        saved = json.loads(consent.consent_path().read_text())
        self.assertEqual(saved["choice"], "contribute")
        self.assertEqual(saved["reporting"], "enabled")

    def test_read_creates_nothing(self):
        saved = consent.load()
        self.assertEqual(saved["state"], "missing")
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["codex.json", "engine.json"],
        )
        self.assertIsNone(sharing.first_use())  # existing config does not infer a choice

    def test_choice_then_first_use_is_none(self):
        self.assertIsNone(sharing.first_use())
        sharing.record_choice("later")
        self.assertIsNone(sharing.first_use())
        self.assertEqual(sharing.adapter_choice(), "later")

    def test_corrupt_consent_is_fault_not_onboarding(self):
        consent.consent_path().parent.mkdir(parents=True, exist_ok=True)
        consent.consent_path().write_text("{broken")
        saved = consent.load()
        self.assertEqual(saved["state"], "corrupt")
        self.assertIsNone(sharing.first_use())

    def test_corrupt_settings_is_fault_not_onboarding(self):
        sharing.configured_path().parent.mkdir(parents=True, exist_ok=True)
        sharing.configured_path().write_text("{broken")
        self.assertIsNone(sharing.first_use())

    def test_profile_shared_consent_path_resolution(self):
        consent.record_choice("later")
        sibling = self.root / "kimi.json"
        sibling.write_text(json.dumps({}))
        with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(sibling)):
            saved = consent.load()
        self.assertEqual(saved["choice"], "later")

    def test_isolated_profile_does_not_inherit(self):
        consent.record_choice("later")
        other = self.root / "other"
        other.mkdir()
        (other / "codex.json").write_text(json.dumps({}))
        with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(other / "codex.json")):
            saved = consent.load()
        self.assertEqual(saved["state"], "missing")


if __name__ == "__main__":
    unittest.main()
