"""A real unframed producer must be terminated at the protocol byte limit."""
import importlib.util
import os
from pathlib import Path
import sys
import signal
import subprocess
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
    @unittest.skipUnless(os.name == 'posix', 'POSIX inherited group contract')
    def test_inherited_grandchild_pipe_cannot_hold_worker_deadline(self):
        # Real processes and inherited pipes. The outer service owns cleanup;
        # the worker must first be able to return its timeout without deadlock.
        script = f'''import sys,os,subprocess
sys.path.insert(0,{str(SCRIPTS)!r})
import process_guard
process_guard.TIMEOUT=0.2
try:
 process_guard.run_codex([sys.executable,'-c',"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); time.sleep(20)"], '')
except TimeoutError:
 print('bounded timeout',flush=True)
'''
        env=dict(os.environ,MINDIE_MAINTENANCE_GROUP='1')
        p=subprocess.Popen([sys.executable,'-c',script],env=env,stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,start_new_session=True)
        try:
            p.wait(timeout=3)
            self.assertEqual(p.returncode,0)
        finally:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            out,_=p.communicate(timeout=2)
        self.assertIn(b'bounded timeout',out)

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
