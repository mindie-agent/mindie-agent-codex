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

    def _write_case_files(self, root, *, leader_wait=False):
        child = root / "descendant.py"
        child.write_text(
            "import os,pathlib,sys,time\n"
            "pidfile=pathlib.Path(sys.argv[1]); ready=pidfile.with_suffix('.tmp')\n"
            "ready.write_text(str(os.getpid()),encoding='ascii'); ready.replace(pidfile)\n"
            "time.sleep(120)\n", encoding="utf-8")
        parent = root / "leader.py"
        parent.write_text(
            "import pathlib,subprocess,sys,time\n"
            "time.sleep(0.75)\n"  # Longer than the operation deadline, before readiness.
            "kwargs={} if sys.argv[3]=='inherited' else {"
            "'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}\n"
            "child=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]],**kwargs)\n"
            "if len(sys.argv)>4:\n"
            " while not pathlib.Path(sys.argv[4]).exists():time.sleep(.01)\n"
            + ("time.sleep(120)\n" if leader_wait else ""), encoding="utf-8")
        runner = root / "runner.py"
        runner.write_text(
            "import json,pathlib,sys,time\n"
            f"sys.path.insert(0,{str(SCRIPTS)!r})\n"
            "import windows_process\n"
            "real_spawn=windows_process.spawn; prepared={}\n"
            "def ready_spawn(*args,**kwargs):\n"
            " started=time.monotonic(); process=real_spawn(*args,**kwargs)\n"
            " try:\n"
            "  deadline=started+20\n"
            "  while not pathlib.Path(sys.argv[-1]).exists():\n"
            "   if process.poll() is not None or time.monotonic()>=deadline:raise RuntimeError('fixture readiness failed')\n"
            "   time.sleep(.01)\n"
            + ("  assert process.poll() is None\n" if leader_wait else
               "  assert process.wait(timeout=max(.01,deadline-time.monotonic()))==0\n") +
            "  prepared.update(seconds=time.monotonic()-started,leader_exit=process.returncode)\n"
            "  return process\n"
            " except BaseException:\n"
            "  windows_process.close_tree(process);process.wait(timeout=5)\n"
            "  raise\n"
            "windows_process.spawn=ready_spawn\n"
            "api,leader=sys.argv[1],sys.argv[2];timed_out=False\n"
            "if api in ('bounded_process','service_launcher'):\n"
            " import bounded_process\n"
            " try:bounded_process.run([sys.executable,leader,*sys.argv[3:]],'',timeout=.5,allow_service=(api=='service_launcher'))\n"
            " except TimeoutError:timed_out=True\n"
            "elif api=='process_guard':\n"
            " import process_guard\n"
            " try:process_guard.run_codex([sys.executable,leader,*sys.argv[3:]],'',timeout=.5)\n"
            " except TimeoutError:timed_out=True\n"
            "else:raise SystemExit('unknown adapter')\n"
            "value=dict(timed_out=timed_out,prepared=prepared)\n"
            "print(json.dumps(value),flush=True)\n"
            f"result=pathlib.Path({str(root / 'result.json')!r});tmp=result.with_suffix('.tmp')\n"
            "tmp.write_text(json.dumps(value),encoding='utf-8');tmp.replace(result)\n"
            f"while not pathlib.Path({str(root / 'finished')!r}).exists():time.sleep(.01)\n",
            encoding="utf-8")
        return child, parent, runner

    def _run_case(self, api, pipe_mode, *, leader_wait=False):
        from tests.process_fixtures import owned_fixture_process
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child, leader, runner = self._write_case_files(root, leader_wait=leader_wait)
            pidfile, release = root / "descendant.pid", root / "release"
            output_path, error_path = root / "stdout", root / "stderr"
            with output_path.open("wb") as stdout, error_path.open("wb") as stderr:
                with owned_fixture_process(
                    [sys.executable, str(runner), api, str(leader), str(child), str(pidfile), pipe_mode, str(release)],
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                ) as process:
                    child_handle = None
                    try:
                        deadline = time.monotonic() + 30  # Independent fixture watchdog.
                        while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue(pidfile.exists(), "real descendant did not start")
                        child_handle = self._open_owned_child(int(pidfile.read_text("ascii")))
                        self.assertTrue(child_handle, "descendant exited before the ownership assertion")
                        self.assertEqual(self.kernel.WaitForSingleObject(child_handle, 0), WAIT_TIMEOUT)
                        release.touch()  # Exact child HANDLE is now held; operation timing may start.
                        result_path = root / "result.json"
                        while not result_path.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue(result_path.exists(), "ownership runner did not finish")
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                        self.assertGreaterEqual(result["prepared"]["seconds"], .75)
                        self.assertEqual(result["prepared"]["leader_exit"], None if leader_wait else 0)
                        self.assertEqual(result["timed_out"], leader_wait)
                        self.assertEqual(self.kernel.WaitForSingleObject(child_handle, 1000), WAIT_OBJECT_0,
                                         "owned descendant survived adapter cleanup")
                        # Runner exit must not be what closes the product's Job.
                        self.assertIsNone(process.poll(), "runner exited before ownership assertion")
                        (root / "finished").touch()
                        process.wait(timeout=max(.01, deadline-time.monotonic()))
                        self.assertEqual(process.returncode, 0)
                    except BaseException as exc:
                        exc.add_note("fixture stdout: " + output_path.read_text(encoding="utf-8", errors="replace")[-8000:])
                        exc.add_note("fixture stderr: " + error_path.read_text(encoding="utf-8", errors="replace")[-8000:])
                        raise
                    finally:
                        self._terminate_owned_child(child_handle)

    def test_bounded_process_owns_descendants_after_timeout_and_normal_exit(self):
        for pipe_mode, leader_wait in (("inherited", False), ("detached", False), ("inherited", True)):
            with self.subTest(pipe_mode=pipe_mode, leader_wait=leader_wait):
                self._run_case("bounded_process", pipe_mode, leader_wait=leader_wait)

    def test_process_guard_owns_descendants_after_timeout_and_normal_exit(self):
        for pipe_mode, leader_wait in (("inherited", False), ("detached", False), ("inherited", True)):
            with self.subTest(pipe_mode=pipe_mode, leader_wait=leader_wait):
                self._run_case("process_guard", pipe_mode, leader_wait=leader_wait)

    def test_service_launcher_still_cleans_ordinary_descendants(self):
        for pipe_mode, leader_wait in (("inherited", False), ("detached", False), ("inherited", True)):
            with self.subTest(pipe_mode=pipe_mode, leader_wait=leader_wait):
                self._run_case("service_launcher", pipe_mode, leader_wait=leader_wait)

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
                deadline = time.monotonic() + 20
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(pidfile.exists(), "negative-control descendant did not start")
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
