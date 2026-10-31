"""Real entry lifetimes; no runtime install, service, network or model requests."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import bootstrap
import windows_process
from bounded_process import run


def ready(path, process=None):
    until = time.monotonic() + 15
    while not path.exists():
        if process is not None and process.poll() is not None:
            raise AssertionError("entry exited before child readiness")
        if time.monotonic() >= until:
            raise AssertionError("fixture readiness deadline")
        time.sleep(.01)


class BootstrapExecTests(unittest.TestCase):
    def test_wait_and_cleanup_failures_preserve_primary_and_known_exit(self):
        primary = OSError("wait failed")
        with patch.object(windows_process.os, "name", "nt"), \
                patch.object(windows_process, "spawn", return_value=Mock(wait=Mock(side_effect=primary))), \
                patch.object(windows_process, "close_tree", side_effect=RuntimeError("cleanup failed")), \
                self.assertRaises(OSError) as caught:
            windows_process.exec_or_wait(["fixture"], {})
        self.assertIs(caught.exception, primary)
        self.assertIn("cleanup also failed: RuntimeError", primary.__notes__[0])
        for returncode in (0, 17):
            with self.subTest(returncode=returncode), \
                    patch.object(windows_process.os, "name", "nt"), \
                    patch.object(windows_process, "spawn", return_value=Mock(wait=Mock(return_value=returncode))), \
                    patch.object(windows_process, "close_tree", side_effect=OSError("cleanup failed")), \
                    self.assertRaises(OSError) as caught:
                windows_process.exec_or_wait(["fixture"], {})
            self.assertEqual(caught.exception.returncode, returncode)

    def test_redirect_waits_for_installer_and_preserves_its_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, release = root / "ready", root / "release"
            cancel = threading.Event()
            errors = []
            output = io.StringIO()
            code = (
                "import sys,time;from pathlib import Path;"
                f"Path({str(marker)!r}).touch();"
                f"\nwhile not Path({str(release)!r}).exists(): time.sleep(.01)"
                "\nprint('PRIVATE_INSTALL_DETAIL',file=sys.stderr);raise SystemExit(17)"
            )

            def installer(argv, payload, **options):
                prefix = argv[:argv.index("-c") + 3]
                from mindie_knowledge.filesystem import path_is_within
                assert path_is_within(prefix[-1], root / "data")
                return run([*prefix, sys.executable, "-c", code], payload,
                           cancel=cancel, **options)

            def prepare():
                try:
                    with patch.object(bootstrap.shutil, "which", return_value=sys.executable), \
                            patch.object(bootstrap, "run", side_effect=installer), \
                            patch.object(bootstrap, "readback", side_effect=AssertionError("false success")), \
                            patch("diagnostic_support.failure", return_value={}), redirect_stderr(output):
                        bootstrap.ensure_runtime(SCRIPTS.parent, root / "data")
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=prepare)
            thread.start()
            try:
                ready(marker)
                self.assertTrue(thread.is_alive(), "redirector claimed completion while installer was active")
                release.touch()
                thread.join(15)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1, output.getvalue())
                result = errors[0].process_result
                self.assertEqual((result.execution, result.returncode, result.cleanup),
                                 ("completed", 17, []))
                self.assertEqual(list((root / "data").rglob("runtime.json")), [])
                logs = list((root / "data").rglob("prepare-*.log"))
                self.assertEqual(len(logs), 1)
                self.assertIn("PRIVATE_INSTALL_DETAIL", logs[0].read_text())
                logs[0].unlink()  # Windows denies this while a writer still holds it.
            finally:
                release.touch()
                cancel.set()
                thread.join(5)

    def test_published_entry_stays_alive_and_preserves_streams_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, release = root / "scripts", root / "release"
            scripts.mkdir()
            release.mkdir()
            shutil.copyfile(SCRIPTS / "windows_process.py", scripts / "windows_process.py")
            marker = root / "ready"
            (scripts / "entry.py").write_text(
                "import sys;from pathlib import Path;"
                f"Path({str(marker)!r}).touch();"
                "data=sys.stdin.readline();print('out:'+data.strip());"
                "print('err:retained',file=sys.stderr);raise SystemExit(17)",
                encoding="utf-8")
            target = bootstrap.publish_entry(Path(sys.executable), scripts, release)
            process = windows_process.spawn([sys.executable, "-I", str(target), "stop"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
            try:
                ready(marker, process)
                self.assertIsNone(process.poll())
                out, err = process.communicate(b"release\n", timeout=15)
                self.assertEqual(process.returncode, 17)
                self.assertEqual(out.strip(), b"out:release")
                self.assertEqual(err.strip(), b"err:retained")
            finally:
                windows_process.close_tree(process)
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    @unittest.skipUnless(os.name == "nt", "Windows exact owned Job lifetime")
    def test_entry_owner_death_stops_its_child(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            child = f"import os,time;from pathlib import Path;Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)"
            owner = (f"import sys,os;sys.path.insert(0,{str(SCRIPTS)!r});"
                     "from windows_process import exec_or_wait;"
                     f"exec_or_wait([sys.executable,'-c',{child!r}],dict(os.environ))")
            process = windows_process.spawn([sys.executable, "-I", "-c", owner])
            handle = None
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            try:
                ready(marker, process)
                handle = kernel.OpenProcess(0x00100000, False, int(marker.read_text()))
                self.assertTrue(handle)
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                process.kill()  # Kill only owner; its inner Job must kill child.
                process.wait(timeout=5)
                self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            finally:
                if handle:
                    kernel.CloseHandle(handle)
                windows_process.close_tree(process)
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
