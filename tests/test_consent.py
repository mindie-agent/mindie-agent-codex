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

    def test_legacy_adapter_choice_imports_once_at_boundary(self):
        adapter = json.loads(self.config.read_text())
        adapter["sharing_choice"] = "read-only"
        self.config.write_text(json.dumps(adapter))
        # Reads never import: the authority stays missing until an explicit
        # install/upgrade/entry boundary migrates it.
        self.assertIsNone(sharing.adapter_choice())
        self.assertFalse(consent.consent_path().exists())
        migrated = consent.migrate_legacy()
        self.assertEqual(migrated["status"], "migrated")
        self.assertEqual(sharing.adapter_choice(), "read-only")
        saved = json.loads(consent.consent_path().read_text())
        self.assertEqual(saved["choice"], "read-only")
        # The adapter key is then ignored as a consent source.
        adapter["sharing_choice"] = "later"
        self.config.write_text(json.dumps(adapter))
        self.assertEqual(sharing.adapter_choice(), "read-only")
        self.assertEqual(consent.migrate_legacy()["status"], "kept")

    def test_conflicting_legacy_sources_never_guess_consent(self):
        adapter = json.loads(self.config.read_text())
        adapter["sharing_choice"] = "read-only"
        self.config.write_text(json.dumps(adapter))
        self.legacy_community.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=True,
                 generation="g", enabled_at=1.0, repository="owner/repo",
                 project_roots=[str(self.root)], idle_seconds=300)
        ))
        migrated = consent.migrate_legacy()
        self.assertEqual(migrated["status"], "conflict")
        self.assertFalse(consent.consent_path().exists())
        self.assertIsNone(sharing.adapter_choice())
        # An explicit user choice still records normally afterwards.
        sharing.record_choice("later")
        self.assertEqual(sharing.adapter_choice(), "later")

    def test_damaged_legacy_source_is_a_fault_not_consent(self):
        self.legacy_community.write_text("{broken")
        migrated = consent.migrate_legacy()
        self.assertEqual(migrated["status"], "error")
        self.assertEqual(migrated["state"], "damaged-legacy")
        self.assertFalse(consent.consent_path().exists())
        # Existing installation: never re-onboarded.
        self.assertIsNone(sharing.first_use())

    def test_record_choice_refuses_to_clear_damaged_consent(self):
        consent.consent_path().write_text("{broken")
        with self.assertRaises(consent.ConsentError) as ctx:
            consent.record_choice("later")
        self.assertEqual(ctx.exception.state, "corrupt")
        self.assertEqual(consent.consent_path().read_text(), "{broken")
        self.assertEqual(consent.load()["state"], "corrupt")

    def test_shared_authority_exists_means_legacy_pointer_is_not_consulted(self):
        # The profile-shared file is the live authority once it exists; a
        # legacy pointer naming an enabled file is never a fallback when the
        # authority is damaged or unreadable.
        shared = consent.shared_community_path()
        shared.write_text("{broken")
        self.legacy_community.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=True,
                 generation="g", enabled_at=1.0, repository="owner/repo",
                 project_roots=[str(self.root)], idle_seconds=300)
        ))
        self.assertIsNone(sharing.read())
        lease = dict(project_root=str(self.root), root_session="t",
                     activated_at=1.0)
        self.assertFalse(sharing.capture_allowed(lease, str(self.root)))

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
        self.assertFalse(sharing.capture_allowed(lease, str(self.root)), "missing")
        authority.write_text("{broken")
        self.assertFalse(sharing.capture_allowed(lease, str(self.root)), "corrupt")
        # The field grants nothing by itself: enabled=false still wins.
        authority.unlink()
        consent.record_choice("contribute")
        settings["enabled"] = False
        settings["enabled_at"] = None
        shared.write_text(json.dumps(settings))
        self.assertFalse(sharing.capture_allowed(lease, str(self.root)))
        # Legacy format without the field keeps previous read compatibility.
        settings["enabled"] = True
        settings["enabled_at"] = 1.0
        del settings["consent_config"]
        shared.write_text(json.dumps(settings))
        authority.unlink()
        self.assertTrue(sharing.capture_allowed(lease, str(self.root)))

    def test_concurrent_field_updates_both_survive(self):
        consent.record_choice("later")
        consent.record_reporting("disabled")
        script = self.root / "race_update.py"
        script.write_text(
            "import os, sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "os.environ['MINDIE_AGENT_CONFIG'] = sys.argv[2]\n"
            "import consent_store\n"
            "real = consent_store._read_raw\n"
            "def slow(path):\n"
            "    found = real(path)\n"
            "    time.sleep(0.6)\n"
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

    def test_migration_stamp_preserves_a_nonconforming_document(self):
        shared = consent.shared_community_path()
        foreign = '{"schema":"foreign-format/1","payload":"preserve"}\n'
        shared.write_text(foreign)
        migrated = sharing.migrate_community_path()
        self.assertEqual(shared.read_text(), foreign)  # bytes preserved
        self.assertIsNotNone(migrated["detail"])  # fault reported, not silent
        # Parseable but malformed managed values: also never stamped.
        shared.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled="yes",
                 project_roots="not-a-list", idle_seconds=300)
        ))
        before = shared.read_text()
        migrated = sharing.migrate_community_path()
        self.assertEqual(shared.read_text(), before)
        self.assertIsNotNone(migrated["detail"])
        # Managed values the adapter pre-filter tolerates but the core's
        # authoritative normalize rejects (idle_seconds below the floor):
        # bytes preserved, fault reported — the stamp is not a repair path.
        shared.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=False,
                 repository="owner/repo", project_roots=[], idle_seconds=1)
        ))
        before = shared.read_text()
        migrated = sharing.migrate_community_path()
        self.assertEqual(shared.read_text(), before)
        self.assertIn("normalize", migrated["detail"])
        # A conforming document is stamped normally, managed keys untouched.
        shared.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=False,
                 repository="owner/repo", project_roots=[], idle_seconds=300)
        ))
        migrated = sharing.migrate_community_path()
        settings = json.loads(shared.read_text())
        self.assertEqual(settings["consent_config"], str(consent.consent_path()))
        self.assertFalse(settings["enabled"])
        self.assertNotIn("enabled_at", settings)  # managed keys untouched

    def test_read_and_noop_migration_create_nothing(self):
        saved = consent.load()
        self.assertEqual(saved["state"], "missing")
        self.assertEqual(consent.migrate_legacy()["status"], "absent")
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["codex.json", "engine.json"],
        )
        self.assertIsNotNone(sharing.first_use())  # genuinely unchosen

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

    def test_legacy_community_file_is_adopted_once_at_boundary(self):
        self.legacy_community.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=False,
                 repository="owner/repo", project_roots=[], idle_seconds=300)
        ))
        shared = consent.shared_community_path()
        self.assertFalse(shared.exists())
        # Reads are pure: no adoption copy, no pointer rewrite, no fallback.
        resolved = sharing.configured_path()
        self.assertEqual(resolved, self.legacy_community)
        self.assertFalse(shared.exists())
        self.assertEqual(
            json.loads(self.config.read_text())["community_config"],
            str(self.legacy_community),
        )
        # The explicit boundary migrates once and repoints adapter+engine.
        migrated = sharing.migrate_community_path()
        self.assertEqual(migrated["status"], "adopted")
        self.assertEqual(sharing.configured_path(), shared)
        self.assertTrue(shared.exists())
        self.assertTrue(self.legacy_community.exists())  # evidence kept
        adapter = json.loads(self.config.read_text())
        self.assertEqual(adapter["community_config"], str(shared))
        engine = json.loads(self.engine.read_text())
        self.assertEqual(engine["community_config"], str(shared))
        # The consent authority pointer is wired into the settings.
        settings = json.loads(shared.read_text())
        self.assertEqual(settings["consent_config"], str(consent.consent_path()))
        # Idempotent: a later legacy write is not re-adopted.
        self.legacy_community.write_text("{changed")
        self.assertEqual(sharing.configured_path(), shared)
        self.assertEqual(sharing.migrate_community_path()["status"], "current")
        self.assertNotEqual(shared.read_text(), "{changed")

    def test_existing_shared_authority_never_merges_legacy_scope(self):
        shared = consent.shared_community_path()
        shared.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=False,
                 repository="owner/repo", project_roots=[], idle_seconds=300)
        ))
        self.legacy_community.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=True,
                 generation="g", enabled_at=1.0, repository="owner/repo",
                 project_roots=[str(self.root)], idle_seconds=300)
        ))
        migrated = sharing.migrate_community_path()
        self.assertEqual(migrated["status"], "repointed")
        self.assertIsNotNone(migrated["detail"])  # conflict diagnosed
        settings = json.loads(shared.read_text())
        self.assertFalse(settings["enabled"])  # shared scope wins unchanged
        self.assertEqual(settings["project_roots"], [])
        self.assertTrue(self.legacy_community.exists())
        adapter = json.loads(self.config.read_text())
        self.assertEqual(adapter["community_config"], str(shared))

    def test_enabled_true_legacy_settings_imports_contribute_at_boundary(self):
        self.legacy_community.write_text(json.dumps(
            dict(schema="mindie-community-config/1", enabled=True,
                 generation="g", enabled_at=1.0, repository="owner/repo",
                 project_roots=[str(self.root)], idle_seconds=300)
        ))
        # Reads stay pure; the explicit boundary performs the one-time import.
        self.assertIsNone(sharing.adapter_choice())
        migrated = consent.migrate_legacy()
        self.assertEqual(migrated["status"], "migrated")
        self.assertEqual(migrated["choice"], "contribute")
        self.assertEqual(sharing.adapter_choice(), "contribute")

    def test_cross_adapter_profile_shares_choice(self):
        consent.record_choice("later")
        kimi_commit = "90f73e76c6087ce091570f2d151b709145c913bc"
        value = os.environ.get("MINDIE_KIMI_REPO")
        if not value:
            raise AssertionError(
                "MINDIE_KIMI_REPO is required to load the kimi adapter at "
                + kimi_commit
                + ". Pass the fixed checkout path. This test does not guess "
                "a sibling directory or a production install."
            )
        kimi_repo = Path(value).expanduser()
        if not kimi_repo.is_dir():
            raise AssertionError("MINDIE_KIMI_REPO=" + value + " is not a directory")
        archive = subprocess.run(
            ["git", "-C", str(kimi_repo), "archive", kimi_commit, "scripts"],
            check=True, capture_output=True,
        )
        extracted = self.root / "kimi-adapter"
        extracted.mkdir()
        subprocess.run(["tar", "-x", "-C", str(extracted)], input=archive.stdout, check=True)
        (self.root / "kimi.json").write_text("{}\n")
        env = dict(os.environ, MINDIE_KIMI_CONFIG=str(self.root / "kimi.json"))
        env.pop("MINDIE_AGENT_CONFIG", None)
        code = (
            "import sys,json;sys.path.insert(0,sys.argv[1]);"
            "import consent;print(json.dumps(consent.load()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(extracted / "scripts")],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(result.stdout)
        self.assertEqual(saved["choice"], "later")

    def test_profile_shared_consent_path_resolution(self):
        # The consent document resolves beside the adapter config, so a
        # sibling adapter in the same profile directory lands on the same
        # file. Simulated with a differently-named adapter config in this
        # directory; the authoritative cross-adapter check loads a real
        # second adapter (grok-codex's contract suite with the kimi adapter).
        consent.record_choice("later")
        sibling = self.root / "kimi.json"
        sibling.write_text(json.dumps({}))
        env = dict(os.environ, MINDIE_AGENT_CONFIG=str(sibling))
        code = (
            "import sys,json;sys.path.insert(0,sys.argv[1]);"
            "import consent;print(json.dumps(consent.load()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(SCRIPTS)],
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
