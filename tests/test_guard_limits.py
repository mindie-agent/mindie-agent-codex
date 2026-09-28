"""A real unframed producer must be terminated at the protocol byte limit."""
import importlib.util
import os
from pathlib import Path
import sys
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
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

    @unittest.skipUnless(os.name == 'nt', 'Windows owned process-tree contract')
    def test_windows_timeout_kills_owned_descendants_and_closes_pipes(self):
        # Replacement coverage for the POSIX-only inherited process-group
        # test above: taskkill /T must stop a real Python grandchild that
        # inherited both pipes, before its marker deadline.
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "grandchild-survived"
            child = (
                "import time\n"
                "from pathlib import Path\n"
                "time.sleep(0.8)\n"
                f"Path({str(marker)!r}).write_text('survived')\n"
            )
            parent = (
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "time.sleep(20)\n"
            )
            start = time.monotonic()
            with patch.object(guard, "TIMEOUT", 0.2), self.assertRaises(TimeoutError):
                guard.run_codex([sys.executable, "-c", parent], "")
            self.assertLess(time.monotonic() - start, 3)
            time.sleep(1)
            self.assertFalse(marker.exists(), "owned grandchild outlived timeout cleanup")

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
