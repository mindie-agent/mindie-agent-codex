"""Native Win32 subprocess ownership, bounded by an outer test watchdog."""

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
ERROR_INVALID_PARAMETER = 87


@unittest.skipUnless(os.name == "nt", "Win32 Job Object contract; POSIX group tests are separate")
class WindowsProcessTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        cls.kernel.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        cls.kernel.OpenProcess.restype = ctypes.c_void_p
        cls.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        cls.kernel.WaitForSingleObject.restype = ctypes.c_ulong
        cls.kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        cls.kernel.TerminateProcess.restype = ctypes.c_int
        cls.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        cls.kernel.CloseHandle.restype = ctypes.c_int

    def _open_owned_child(self, pid):
        handle = self.kernel.OpenProcess(
            SYNCHRONIZE | PROCESS_TERMINATE,
            False,
            pid,
        )
        if handle:
            return handle
        error = ctypes.get_last_error()
        if error == ERROR_INVALID_PARAMETER:
            return None  # The process has already exited; this is read-only.
        raise ctypes.WinError(error)

    def _terminate_owned_child(self, handle):
        if handle:
            if self.kernel.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT:
                self.kernel.TerminateProcess(handle, 1)
                self.kernel.WaitForSingleObject(handle, 2000)
            self.kernel.CloseHandle(handle)

    def _write_case_files(self, root):
        child = root / "descendant.py"
        child.write_text("import time; time.sleep(30)\n", encoding="utf-8")
        parent = root / "leader.py"
        parent.write_text(
            "import pathlib, subprocess, sys\n"
            "kwargs = {} if sys.argv[3] == 'inherited' else {"
            "'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}\n"
            "child = subprocess.Popen([sys.executable, sys.argv[1]], **kwargs)\n"
            "pathlib.Path(sys.argv[2]).write_text(str(child.pid), encoding='ascii')\n",
            encoding="utf-8",
        )
        runner = root / "runner.py"
        runner.write_text(
            "import json, sys\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "api, leader = sys.argv[1], sys.argv[2]\n"
            "timed_out = False\n"
            "if api in ('bounded_process', 'service_launcher'):\n"
            " import bounded_process\n"
            " try: bounded_process.run([sys.executable, leader, *sys.argv[3:]], '', timeout=0.5, allow_service=(api == 'service_launcher'))\n"
            " except TimeoutError: timed_out = True\n"
            "elif api == 'process_guard':\n"
            " import process_guard\n"
            " process_guard.TIMEOUT = 0.5\n"
            " try: process_guard.run_codex([sys.executable, leader, *sys.argv[3:]], '')\n"
            " except TimeoutError: timed_out = True\n"
            "else: raise SystemExit('unknown adapter')\n"
            "print(json.dumps({'timed_out': timed_out}), flush=True)\n",
            encoding="utf-8",
        )
        return child, parent, runner

    def _run_case(self, api, pipe_mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child, leader, runner = self._write_case_files(root)
            pidfile = root / "descendant.pid"
            started = time.monotonic()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    api,
                    str(leader),
                    str(child),
                    str(pidfile),
                    pipe_mode,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            child_handle = None
            try:
                deadline = started + 2
                while (
                    (not pidfile.exists() or pidfile.stat().st_size == 0)
                    and process.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertTrue(pidfile.exists(), "real descendant did not start")
                child_handle = self._open_owned_child(int(pidfile.read_text("ascii")))
                stdout, stderr = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace"))
                result = json.loads(stdout)
                self.assertEqual(result["timed_out"], pipe_mode == "inherited")
                if child_handle:
                    self.assertEqual(
                        self.kernel.WaitForSingleObject(child_handle, 1000),
                        WAIT_OBJECT_0,
                        "owned descendant survived adapter cleanup",
                    )
                self.assertLess(time.monotonic() - started, 3)
            finally:
                self._terminate_owned_child(child_handle)
                if process.poll() is None:
                    process.kill()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=3)

    def test_bounded_process_owns_descendants_after_timeout_and_normal_exit(self):
        for pipe_mode in ("inherited", "detached"):
            with self.subTest(pipe_mode=pipe_mode):
                self._run_case("bounded_process", pipe_mode)

    def test_process_guard_owns_descendants_after_timeout_and_normal_exit(self):
        for pipe_mode in ("inherited", "detached"):
            with self.subTest(pipe_mode=pipe_mode):
                self._run_case("process_guard", pipe_mode)

    def test_service_launcher_still_cleans_ordinary_descendants(self):
        for pipe_mode in ("inherited", "detached"):
            with self.subTest(pipe_mode=pipe_mode):
                self._run_case("service_launcher", pipe_mode)

    def test_legacy_live_leader_check_is_caught_by_inherited_pipe_control(self):
        """The pre-Job taskkill check misses the tree once its leader exits."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child, leader, _runner = self._write_case_files(root)
            pidfile = root / "descendant.pid"
            process = subprocess.Popen(
                [sys.executable, str(leader), str(child), str(pidfile), "inherited"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            child_handle = None
            try:
                self.assertEqual(process.wait(timeout=2), 0)
                child_handle = self._open_owned_child(int(pidfile.read_text("ascii")))
                self.assertIsNotNone(child_handle, "negative control child exited too early")
                # This is the old cleanup condition: taskkill is skipped after
                # the leader has exited, even though its descendant owns pipes.
                if process.poll() is None:
                    subprocess.run(
                        [
                            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"),
                            "/F",
                            "/T",
                            "/PID",
                            str(process.pid),
                        ],
                        check=False,
                        timeout=1,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                self.assertEqual(
                    self.kernel.WaitForSingleObject(child_handle, 0),
                    WAIT_TIMEOUT,
                    "negative control no longer reproduces the old leak",
                )
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.5)
            finally:
                self._terminate_owned_child(child_handle)
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
