"""The setup/switch helper refuses an unsupported feed before opening Store."""
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from mindie_knowledge import git_layout
from mindie_knowledge.loop import store

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


class PrepareStoreLayoutTests(unittest.TestCase):
    def test_layout_failure_is_visible_before_store_or_config_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ("a" * 90) / ("b" * 90)
            payload = dict(root=str(root), domain="test",
                           feeds=[dict(domain="test", repository="org/feed")])
            output = io.StringIO()
            with patch.object(git_layout, "WINDOWS", True), \
                    patch.object(store, "Store", side_effect=AssertionError("Store opened")), \
                    patch.object(sys, "argv", [str(SCRIPTS / "capture_config.py"), "prepare-store"]), \
                    patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                    patch.object(sys, "stdout", output), \
                    patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(SCRIPTS / "capture_config.py"), run_name="__main__")
            self.assertEqual(raised.exception.code, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["stage"], "prepare-store")
            self.assertEqual(result["error_type"], "UnsupportedGitLayout")
            self.assertIn("unsupported_windows_git_root", result["error"])
            self.assertIn("execution=not_started", result["error"])
            self.assertNotIn(str(root), result["error"])
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
