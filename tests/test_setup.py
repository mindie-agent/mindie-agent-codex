"""Focused checks for the packaging entry: bounded dependency probe before writes."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import setup as setup_script


def run_setup(python, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "setup.py"), "--knowledge-python", str(python), *args],
        text=True,
        capture_output=True,
        timeout=30,
    )


class SetupTests(unittest.TestCase):
    def test_missing_dependencies_fail_clearly_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Isolate the missing-dependency environment from the test runner.
            bare = base / "bare-runtime"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(bare)],
                check=True,
                timeout=30,
                capture_output=True,
            )
            python = bare / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            config = base / "codex.json"
            result = run_setup(python, "--config", config, "--root", base / "data")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing pinned dependencies", result.stderr + result.stdout)
            self.assertIn("remote_dev", result.stderr + result.stdout)
            # Nothing may be written when the probe fails.
            self.assertEqual(list(base.iterdir()), [bare])

    def test_probe_includes_runtime_packages(self):
        self.assertEqual(
            set(setup_script.PROBE_MODULES),
            {
                "mindie_knowledge.loop.cli",
                "mindie_knowledge.loop.documents",
                "mindie_knowledge.loop.activation",
                "remote_dev.mcp.server",
            },
        )
        self.assertGreater(setup_script.PROBE_TIMEOUT, 0)

    def test_complete_runtime_writes_private_config_and_refuses_overwrite(self):
        for module in setup_script.PROBE_MODULES:
            try:
                __import__(module)
            except ImportError:
                self.skipTest(f"pinned runtime not installed in {sys.executable}")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "codex.json"
            result = run_setup(sys.executable, "--config", config, "--root", base / "data")
            self.assertEqual(result.returncode, 0, result.stderr)
            engine = config.with_name("codex.engine.json")
            if os.name == "posix":
                self.assertEqual((config.stat().st_mode & 0o777), 0o600)
                self.assertEqual((engine.stat().st_mode & 0o777), 0o600)
            else:
                # Windows mode bits do not describe the inherited profile
                # ACL; file ACL review remains part of native acceptance.
                self.assertTrue(config.is_file())
                self.assertTrue(engine.is_file())
            value = json.loads(engine.read_text())
            adapter = json.loads(config.read_text())
            self.assertEqual(value["domain"], "vllm-ascend")
            self.assertTrue(value["agent_command"][1].endswith("agent_worker.py"))
            self.assertEqual(
                Path(value["agent_command"][0]).resolve(),
                Path(sys.executable).resolve(),
            )
            self.assertNotIn("session_activation", value)
            self.assertNotIn("session_activation", adapter)
            admission = config.with_name("codex.admission.sqlite3")
            self.assertEqual(value["admission_path"], str(admission))
            self.assertEqual(adapter["admission_path"], str(admission))
            self.assertTrue(Path(value["transcript_adapter"]).is_absolute())
            self.assertTrue(value["transcript_adapter"].endswith("codex_transcript.py"))
            self.assertEqual(adapter["runtime_scripts"], str(SCRIPTS))
            self.assertNotIn("sharing_choice", adapter)
            # Community sharing defaults OFF: the pointer exists, the file not.
            community = config.with_name("mindie-community.json")
            self.assertEqual(value["community_config"], str(community))
            self.assertFalse(community.exists())
            self.assertFalse(admission.exists())
            again = run_setup(sys.executable, "--config", config, "--root", base / "data")
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("configuration already exists", again.stderr)
            # Post-install configure must work even though engine config exists
            # and must drop the retired session_activation alias.
            engine_value = json.loads(engine.read_text())
            engine_value["session_activation"] = str(config)
            engine.write_text(json.dumps(engine_value))
            scope = base / "scope"
            scope.mkdir()
            configured = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "setup.py"),
                    "configure",
                    "--config",
                    str(config),
                    "--community-repository",
                    "mindie-agent/knowledge",
                    "--community-project-root",
                    str(scope),
                    "--community-visibility",
                    "public",
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)
            settings = json.loads(community.read_text())
            self.assertTrue(settings["enabled"])
            # The explicit choice lands in the consent authority; the retired
            # adapter-config key is gone and the gate pointer is wired.
            adapter = json.loads(config.read_text())
            self.assertNotIn("sharing_choice", adapter)
            consent_doc = json.loads(
                config.with_name("mindie-consent.json").read_text()
            )
            self.assertEqual(consent_doc["choice"], "contribute")
            self.assertEqual(
                settings["consent_config"],
                str(config.with_name("mindie-consent.json")),
            )
            self.assertNotIn("session_activation", json.loads(engine.read_text()))

    def test_configure_preserves_a_damaged_community_document(self):
        for module in setup_script.PROBE_MODULES:
            try:
                __import__(module)
            except ImportError:
                self.skipTest(f"pinned runtime not installed in {sys.executable}")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "codex.json"
            result = run_setup(sys.executable, "--config", config, "--root", base / "data")
            self.assertEqual(result.returncode, 0, result.stderr)
            scope = base / "scope"
            scope.mkdir()
            community = config.with_name("mindie-community.json")
            community.write_text("{broken-community")
            before = community.read_bytes()
            configured = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "setup.py"),
                    "configure",
                    "--config", str(config),
                    "--community-repository", "owner/explicit",
                    "--community-project-root", str(scope),
                    "--community-visibility", "public",
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(configured.returncode, 0)
            self.assertIn("damaged", configured.stderr)
            self.assertEqual(community.read_bytes(), before)
            # A parseable document with malformed managed values is the
            # explicit repair path: configure rewrites it normally.
            community.write_text(json.dumps(
                dict(schema="mindie-community-config/1", enabled="yes",
                     project_roots="not-a-list", idle_seconds=300,
                     sibling_key={"owned": "extension"})
            ))
            repaired = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "setup.py"),
                    "configure",
                    "--config", str(config),
                    "--community-repository", "owner/explicit",
                    "--community-project-root", str(scope),
                    "--community-visibility", "public",
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            settings = json.loads(community.read_text())
            self.assertTrue(settings["enabled"])
            self.assertEqual(settings["repository"], "owner/explicit")
            self.assertEqual(settings["sibling_key"], {"owned": "extension"})

    def test_community_selection_records_settings_and_enables_sharing(self):
        for module in setup_script.PROBE_MODULES:
            try:
                __import__(module)
            except ImportError:
                self.skipTest(f"pinned runtime not installed in {sys.executable}")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scope = base / "scope"
            scope.mkdir()
            config = base / "codex.json"
            result = run_setup(
                sys.executable,
                "--config",
                config,
                "--root",
                base / "data",
                "--community-repository",
                "mindie-agent/knowledge",
                "--community-project-root",
                str(scope),
                "--community-account",
                "contributor-1",
                "--community-visibility",
                "public",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            community = config.with_name("mindie-community.json")
            if os.name == "posix":
                self.assertEqual((community.stat().st_mode & 0o777), 0o600)
            else:
                # Windows inherits the temporary profile directory ACL; this
                # test does not claim that the ACL is restrictive.
                self.assertTrue(community.is_file())
            settings = json.loads(community.read_text())
            self.assertEqual(settings["schema"], "mindie-community-config/1")
            self.assertTrue(settings["enabled"])
            self.assertEqual(settings["repository"], "mindie-agent/knowledge")
            self.assertEqual(settings["project_roots"], [str(scope.resolve())])
            self.assertEqual(settings["account"], "contributor-1")
            self.assertEqual(settings["visibility"], "public")
            self.assertGreater(settings["enabled_at"], 0)
            # The consent authority pointer is wired at install.
            self.assertEqual(
                settings["consent_config"],
                str(config.with_name("mindie-consent.json")),
            )
            consent_doc = json.loads(
                config.with_name("mindie-consent.json").read_text()
            )
            self.assertEqual(consent_doc["choice"], "contribute")
            self.assertNotIn("token", community.read_text().lower())

    def test_partial_community_selection_fails_before_any_write(self):
        for module in setup_script.PROBE_MODULES:
            try:
                __import__(module)
            except ImportError:
                self.skipTest(f"pinned runtime not installed in {sys.executable}")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "codex.json"
            # Repository without scope/visibility is refused.
            result = run_setup(
                sys.executable,
                "--config",
                config,
                "--root",
                base / "data",
                "--community-repository",
                "mindie-agent/knowledge",
            )
            self.assertNotEqual(result.returncode, 0)
            # A missing project root directory is refused as well.
            result = run_setup(
                sys.executable,
                "--config",
                config,
                "--root",
                base / "data",
                "--community-repository",
                "mindie-agent/knowledge",
                "--community-project-root",
                str(base / "missing"),
                "--community-visibility",
                "public",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
