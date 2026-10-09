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
    @unittest.skipUnless(os.name == 'posix', 'POSIX owner disappearance')
    def test_owner_exit_reaps_a_quiet_native_without_an_elapsed_deadline(self):
        self._owner_exit_case(close_pipes=False)

    @unittest.skipUnless(os.name == 'posix', 'POSIX owner disappearance after EOF')
    def test_owner_exit_remains_observable_after_native_output_eof(self):
        self._owner_exit_case(close_pipes=True)

    def _owner_exit_case(self, *, close_pipes):
        with tempfile.TemporaryDirectory() as directory:
            ready, survived = Path(directory) / 'ready', Path(directory) / 'survived'
            prefix = 'import os; os.close(1); os.close(2); ' if close_pipes else ''
            native = (prefix + f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); "
                      f"time.sleep(1); Path({str(survived)!r}).touch(); time.sleep(20)")
            worker = (f"import sys; sys.path.insert(0,{str(SCRIPTS)!r}); import process_guard; "
                      f"process_guard.run_codex([sys.executable,'-c',{native!r}], '')")
            owner = ("import os,sys,subprocess,time; "
                     "env=dict(os.environ,MINDIE_MAINTENANCE_GROUP='1',MINDIE_MAINTENANCE_OWNER=str(os.getpid())); "
                     f"p=subprocess.Popen([sys.executable,'-c',{worker!r}],env=env,start_new_session=True); "
                     "print(p.pid,flush=True); time.sleep(20)")
            process = subprocess.Popen([sys.executable, '-c', owner], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            worker_pid = int(process.stdout.readline())
            try:
                until = time.monotonic()+3
                while not ready.exists() and time.monotonic() < until:
                    time.sleep(.02)
                self.assertTrue(ready.exists())
                process.kill()
                process.wait(timeout=2)
                time.sleep(1.2)
                self.assertFalse(survived.exists(), 'native outlived its owner')
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=2)
                try:
                    os.killpg(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_native_eof_waits_for_real_exit_beyond_old_cleanup_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            done = Path(directory) / 'done'
            result = guard.run_codex([sys.executable, '-c',
                'import os,sys,time; from pathlib import Path; os.close(1); os.close(2); '
                'time.sleep(2.2); Path(sys.argv[1]).write_text("done")', str(done)], '')
            self.assertIsNone(result)
            self.assertEqual(done.read_text(), 'done')

    def test_explicit_deadline_remains_live_after_native_eof(self):
        with self.assertRaises(TimeoutError):
            guard.run_codex([sys.executable, '-c',
                'import os,time; os.close(1); os.close(2); time.sleep(30)'], '', timeout=.3)

    @unittest.skipUnless(os.name == 'posix', 'POSIX inherited group contract')
    def test_inherited_grandchild_pipe_cannot_hold_worker_deadline(self):
        # Real processes and inherited pipes. The outer service owns cleanup;
        # the worker must first be able to return its timeout without deadlock.
        script = f'''import sys,os,subprocess
sys.path.insert(0,{str(SCRIPTS)!r})
import process_guard
try:
 process_guard.run_codex([sys.executable,'-c',"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); time.sleep(20)"], '', timeout=0.2)
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
            with self.assertRaises(TimeoutError):
                guard.run_codex([sys.executable, "-c", parent], "", timeout=0.2)
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
