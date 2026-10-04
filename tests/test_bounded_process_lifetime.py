"""EOF is a pipe state, not a completed process or a cancellation boundary."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import bounded_process


@unittest.skipUnless(os.name == 'posix', 'Uses POSIX-owned children; native Windows ownership has separate tests')
class PipeLifetimeTests(unittest.TestCase):
    def runners(self):
        # Exercise both pipe-reading algorithms on the current OS. This does
        # not establish native Windows Job Object acceptance.
        return (bounded_process._run_posix, bounded_process._run_windows)

    def child(self, code, *args):
        return bounded_process._spawn([sys.executable, '-c', code, *map(str, args)], subprocess.DEVNULL, None)

    def test_cancellation_stays_live_after_both_pipes_close(self):
        for runner in self.runners():
            for timeout in (None, 5):
                with self.subTest(reader=runner.__name__, timeout=timeout), tempfile.TemporaryDirectory() as directory:
                    ready = Path(directory) / 'closed'
                    process = self.child(
                        "import os,sys,time;from pathlib import Path;os.close(1);os.close(2);"
                        "Path(sys.argv[1]).write_text('closed');time.sleep(2)", ready)
                    cancel = threading.Event()
                    def after_eof():
                        until = time.monotonic() + 2
                        while not ready.exists() and time.monotonic() < until:
                            time.sleep(.005)
                        cancel.set()
                    trigger = threading.Thread(target=after_eof)
                    trigger.start()
                    started = time.monotonic()
                    try:
                        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                            runner(process, timeout, 1024, cancel)
                    finally:
                        trigger.join(timeout=3)
                        bounded_process._kill_tree(process)
                        process.wait(timeout=3)
                    self.assertTrue(ready.exists())
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertIsNotNone(process.returncode)

    def test_closed_pipes_without_cancel_still_wait_for_successful_exit(self):
        for runner in self.runners():
            with self.subTest(reader=runner.__name__):
                process = self.child('import os,time;os.close(1);os.close(2);time.sleep(.2)')
                started = time.monotonic()
                self.assertEqual(runner(process, None, 1024, None).checked_stdout(), '')
                self.assertGreaterEqual(time.monotonic() - started, .18)
                self.assertEqual(process.returncode, 0)

    def test_target_signal_return_code_is_preserved(self):
        result = bounded_process.run([sys.executable, '-c', 'import os,signal;os.kill(os.getpid(),signal.SIGTERM)'],
                                     '', allowed_returncodes=None)
        self.assertEqual(result.returncode, -15)

    def test_explicit_deadline_still_applies_after_pipe_eof(self):
        for runner in self.runners():
            with self.subTest(reader=runner.__name__):
                process = self.child('import os,time;os.close(1);os.close(2);time.sleep(2)')
                with self.assertRaisesRegex(TimeoutError, 'deadline exceeded'):
                    runner(process, .15, 1024, None)
                self.assertIsNotNone(process.returncode)

    def test_owner_exit_reaps_descendants_holding_output_pipes(self):
        for runner in self.runners():
            with self.subTest(reader=runner.__name__):
                process = self.child("import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(2)'])")
                started = time.monotonic()
                self.assertEqual(runner(process, None, 1024, None).checked_stdout(), '')
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(process.returncode, 0)

    def test_owner_death_after_target_exit_still_cancels_descendants(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            held, child_ready, owner_ready = base / 'held', base / 'child-ready', base / 'owner-ready'
            child = ('import fcntl,time;from pathlib import Path;'
                     f'stream=open({str(held)!r},"w");fcntl.flock(stream,fcntl.LOCK_EX);'
                     f'Path({str(child_ready)!r}).touch();time.sleep(30)')
            target = ('import subprocess,sys,time;from pathlib import Path;'
                      f'subprocess.Popen([sys.executable,"-c",{child!r}]);'
                      f'\nwhile not Path({str(child_ready)!r}).exists(): time.sleep(.01)')
            owner_code = ('import sys,time;from pathlib import Path;'
                          f'sys.path.insert(0,{str(SCRIPTS)!r});from bounded_process import _spawn;'
                          f'p=_spawn([sys.executable,"-c",{target!r}],None,None);p.wait();'
                          f'Path({str(owner_ready)!r}).write_text(str(p.pid));time.sleep(30)')
            owner = subprocess.Popen([sys.executable, '-c', owner_code],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                until = time.monotonic() + 5
                while not owner_ready.exists() and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(owner_ready.exists(), 'target did not finish')
                with held.open() as lease:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    owner.kill(); owner.wait(timeout=3)
                    until = time.monotonic() + 3
                    while True:
                        try:
                            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= until:
                                self.fail('descendant survived owner death after target exit')
                            time.sleep(.01)
            finally:
                if owner.poll() is None:
                    owner.kill(); owner.wait(timeout=3)
                if owner_ready.exists():
                    try:
                        os.killpg(int(owner_ready.read_text()), 9)
                    except ProcessLookupError:
                        pass


if __name__ == '__main__':
    unittest.main()
