"""A real unframed producer must be terminated at the protocol byte limit."""
import importlib.util
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
spec = importlib.util.spec_from_file_location(
    "mindie_guard_limit_case", SCRIPTS / "process_guard.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardLimitTests(unittest.TestCase):
    def test_no_newline_output_is_bounded_and_stopped(self):
        environment = dict(os.environ)
        environment.pop("MINDIE_MAINTENANCE_GROUP", None)
        start = time.monotonic()
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "exceeds limit"):
                guard.run_codex(
                    [sys.executable, "-c", 'import os,time; os.write(1,b"x"*1048576); time.sleep(10)'],
                    "",
                )
        self.assertLess(time.monotonic() - start, 3)
