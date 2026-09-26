"""F2 consent authority for the Codex adapter: profile-shared persistent
choice, legacy migration (adapter sharing_choice + legacy community file),
damaged-state honesty. Real files, stub runtime; no model, no network."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        self.legacy_community = self.root / "codex.community.json"
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
        self.temp.cleanup()

    def test_legacy_adapter_choice_imports_once(self):
        adapter = json.loads(self.config.read_text())
        adapter["sharing_choice"] = "read-only"
        self.config.write_text(json.dumps(adapter))
        self.assertEqual(sharing.adapter_choice(), "read-only")
        saved = json.loads(consent.consent_path().read_text())
        self.assertEqual(saved["choice"], "read-only")
        # The adapter key is then ignored as a consent source.
        adapter["sharing_choice"] = "later"
        self.config.write_text(json.dumps(adapter))
        self.assertEqual(sharing.adapter_choice(), "read-only")

    def test_choice_then_first_use_is_none(self):
        self.assertIsNotNone(sharing.first_use())
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

    def test_legacy_community_file_is_adopted_once(self):
        self.legacy_community.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=False,
                 repository="owner/repo", project_roots=[], idle_seconds=300)
        ))
        shared = consent.shared_community_path()
        self.assertFalse(shared.exists())
        resolved = sharing.configured_path()
        self.assertEqual(resolved, shared)
        self.assertTrue(shared.exists())
        self.assertTrue(self.legacy_community.exists())  # evidence kept
        adapter = json.loads(self.config.read_text())
        self.assertEqual(adapter["community_config"], str(shared))
        # Idempotent: a later legacy write is not re-adopted.
        self.legacy_community.write_text("{changed")
        self.assertEqual(sharing.configured_path(), shared)
        self.assertNotEqual(shared.read_text(), "{changed")

    def test_enabled_true_legacy_settings_imports_contribute(self):
        self.legacy_community.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=True,
                 generation="g", enabled_at=1.0, repository="owner/repo",
                 project_roots=[str(self.root)], idle_seconds=300)
        ))
        self.assertEqual(sharing.adapter_choice(), "contribute")

    def test_cross_adapter_profile_shares_choice(self):
        consent.record_choice("later")
        kimi_scripts = ROOT.parent / "kimi" / "scripts"
        (self.root / "kimi.json").write_text(json.dumps({}))
        env = dict(os.environ, MINDIE_KIMI_CONFIG=str(self.root / "kimi.json"))
        env.pop("MINDIE_AGENT_CONFIG", None)
        code = (
            "import sys,json;sys.path.insert(0,sys.argv[1]);"
            "import consent;print(json.dumps(consent.load()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(kimi_scripts)],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(result.stdout)
        self.assertEqual(saved["choice"], "later")

    def test_isolated_profile_does_not_inherit(self):
        consent.record_choice("later")
        other = self.root / "other"
        other.mkdir()
        (other / "codex.json").write_text(json.dumps({}))
        env = dict(os.environ, MINDIE_AGENT_CONFIG=str(other / "codex.json"))
        code = (
            "import sys,json;sys.path.insert(0,sys.argv[1]);"
            "import consent;print(json.dumps(consent.load()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(SCRIPTS)],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "missing")


if __name__ == "__main__":
    unittest.main()
