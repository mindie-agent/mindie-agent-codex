"""Offline log retention does not depend on consent to upload faults."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.process_fixtures import cleanup_temporary_directory

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
from auto_update import Updater


class DiagnosticMaintenanceTests(unittest.TestCase):
    def test_local_retention_runs_but_reporter_handoff_needs_saved_consent(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temporary_directory, temporary)
        root = Path(temporary.name)
        config = root / "codex.json"
        config.write_text(json.dumps({"python": sys.executable}))
        settings = root / "updater.json"
        settings.write_text(json.dumps({"root": str(root / "updates"),
                                        "adapter_config": str(config)}))
        for choice in (None, "later", "disabled", "enabled"):
            with self.subTest(choice=choice):
                updater = Updater(settings)
                with patch("consent.load", return_value={"reporting": choice}), patch.object(
                    updater, "command", return_value='{"status":"ok","network":false}'
                ) as command:
                    result = updater.maintain_diagnostics()
                self.assertEqual(result, {"status": "ok", "network": False})
                args = command.call_args.args[0]
                self.assertEqual(args[:5], [sys.executable, "-m", "mindie_diagnostics.cli", "reporting", "maintain"])
                self.assertEqual("--update-running" in args, choice == "enabled")
                self.assertNotIn("ensure", args)
                self.assertNotIn("configure", args)
                self.assertFalse((root / "mindie-consent.json").exists())
