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
    def test_owned_command_can_declare_unicode_output_encoding(self):
        sys.path.insert(0, str(SCRIPTS))
        from bounded_process import run

        text = "\u5df2\u521b\u5efa\u94fe\u63a5"
        command = [sys.executable, "-c",
                   f"import sys;sys.stdout.buffer.write({text!r}.encode('utf-16-le'))"]
        result = run(command, "", output_encoding="utf-16-le")
        self.assertEqual(result.checked_stdout(), text)
        self.assertEqual(result.returncode, 0)

    @unittest.skipUnless(os.name == "nt", "actual native Windows junction")
    def test_junction_creation_with_unicode_paths_and_output(self):
        sys.path.insert(0, str(SCRIPTS))
        from auto_update import link, _remove_link

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "\u5de5\u4f5c path"
            target = root / "\u6e90 target"
            target.mkdir(parents=True)
            (target / "marker").write_text("preserved", encoding="utf-8")
            selected = root / "\u9009\u4e2d link"
            try:
                link(selected, target)
                self.assertEqual((selected / "marker").read_text(encoding="utf-8"), "preserved")
                self.assertFalse(selected.with_name(selected.name + ".next").exists())
            finally:
                _remove_link(selected.with_name(selected.name + ".next"))
                _remove_link(selected)
            self.assertEqual((target / "marker").read_text(encoding="utf-8"), "preserved")

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
