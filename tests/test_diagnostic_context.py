"""Explicit diagnostic paths survive native environment filtering and updates."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import diagnostic_support


class DiagnosticContextTests(unittest.TestCase):
    def test_only_explicit_paths_are_captured_and_missing_context_preserves_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            selected = {
                "MINDIE_DIAGNOSTICS_CONFIG": str(base / "policy.json"),
                "MINDIE_DIAGNOSTICS_ROOT": str(base / "events"),
            }
            source = dict(selected, GITHUB_TOKEN="never-copy", OTHER_CONFIG="unrelated")
            self.assertEqual(diagnostic_support.capture_environment_context(source), selected)
            self.assertEqual(diagnostic_support.capture_environment_context({}), {})
            untouched = dict(source)
            diagnostic_support.apply_environment_context({}, untouched)
            self.assertEqual(untouched, source)
            self.assertFalse((base / "policy.json").exists())
            self.assertFalse((base / "events").exists())

    def test_invalid_saved_context_is_not_treated_as_the_shared_default(self):
        for value in (None, [], {"GITHUB_TOKEN": "secret"},
                      {"MINDIE_DIAGNOSTICS_ROOT": "relative"},
                      {"MINDIE_DIAGNOSTICS_CONFIG": False}):
            with self.subTest(context=value):
                environment = {"unchanged": "sentinel"}
                with self.assertRaises(ValueError):
                    diagnostic_support.apply_environment_context(
                        {"diagnostics_context": value}, environment)
                self.assertEqual(environment, {"unchanged": "sentinel"})

    def test_context_document_is_bounded_and_invalid_bytes_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "adapter.json"
            self.assertEqual(diagnostic_support.read_environment_context(config), {})
            for content in ("{", "null", " " * 65537):
                config.write_text(content)
                with self.assertRaises(ValueError):
                    diagnostic_support.read_environment_context(config)
            config.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(config)
                with self.assertRaises(ValueError):
                    diagnostic_support.read_environment_context(config)



if __name__ == "__main__":
    unittest.main()
