"""Explicit profile consent and concurrent update tests."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.process_fixtures import cleanup_temporary_directory

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

    def test_explicit_choice_preserves_a_damaged_authority(self):
        consent.consent_path().write_text("{broken")
        with self.assertRaises(consent.ConsentError):
            consent.record_choice("later")
        self.assertEqual(consent.consent_path().read_text(), "{broken")
        self.assertEqual(consent.load()["state"], "corrupt")

    def test_consent_config_gates_the_capture_write_path(self):
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
        consent.record_choice("contribute")
        settings["enabled"] = False
        settings["enabled_at"] = None
        shared.write_text(json.dumps(settings))
        self.assertFalse(sharing.capture_allowed(lease, str(self.root)))


    def test_read_creates_nothing(self):
        saved = consent.load()
        self.assertEqual(saved["state"], "missing")
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["codex.json", "engine.json"],
        )
        self.assertIsNone(sharing.first_use())

    def test_choice_then_first_use_is_none(self):
        self.assertIsNone(sharing.first_use())
        sharing.record_choice("later")
        self.assertIsNone(sharing.first_use())
        self.assertEqual(sharing.adapter_choice(), "later")

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
