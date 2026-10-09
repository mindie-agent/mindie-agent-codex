"""Actual setup files must be UTF-8 even under a legacy Windows locale."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'


class UnicodeInstallTests(unittest.TestCase):
    def test_setup_and_core_readback_with_unicode_paths(self):
        from mindie_knowledge.loop.cli import config_at
        from tests.test_setup import run_setup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "\u5de5\u4f5c\U00020000"
            root.mkdir()
            config = root / "adapter.json"
            env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp1252",
                       MINDIE_AGENT_CONFIG=str(config), MINDIE_KIMI_CONFIG=str(config))
            # The same setup writes run under the legacy locale; publication
            # verification is isolated as in other config-write tests.
            result = run_setup(sys.executable, "--config", str(config),
                               "--root", str(root / "runtime"), "--no-public-feed", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            adapter = json.loads(config.read_bytes())
            engine = config_at(adapter["engine_config"])
            self.assertIn(str(root), engine["root"])
            status = subprocess.run([sys.executable, str(SCRIPTS / "bridge.py"), "status"],
                                    env=env, capture_output=True, timeout=10)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIsInstance(json.loads(status.stdout), dict)


if __name__ == "__main__":
    unittest.main()
