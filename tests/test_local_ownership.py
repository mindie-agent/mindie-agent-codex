"""Actual OS children for local ownership APIs. No weights, models, or network."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = ROOT / "plugins/mindie-agent/domain-lib"
sys.path[:0] = [str(DOMAIN)]

import mindie_exec  # noqa: E402


SLEEP_CHILD = (
    "import os, subprocess, sys, time\n"
    "root = sys.argv[1]\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "open(root + '/pids', 'w', encoding='utf-8').write(f'{os.getpid()} {child.pid}\\n')\n"
    "time.sleep(30)\n"
)


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not mindie_exec.pid_alive(pid):
            return True
        time.sleep(0.02)
    return not mindie_exec.pid_alive(pid)


class LocalOwnershipTests(unittest.TestCase):
    def test_identity_uses_start_time_and_argv_not_pid_alone(self) -> None:
        marker = f"mindie-identity-{time.time_ns()}"
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time,sys; time.sleep(20); sys.argv"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 2
            identity = None
            while time.monotonic() < deadline:
                identity = mindie_exec.process_identity(proc.pid)
                if identity:
                    break
                time.sleep(0.02)
            self.assertIsInstance(identity, dict)
            self.assertTrue(identity["started"])
            self.assertIn("python", identity["command"].lower())
            self.assertTrue(mindie_exec.pid_alive(proc.pid))
            first = identity
            proc.kill()
            proc.wait(timeout=3)
            self.assertFalse(mindie_exec.pid_alive(proc.pid))
            self.assertIsNone(mindie_exec.process_identity(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

        later = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep(20)  # {marker}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 2
            second = None
            while time.monotonic() < deadline:
                second = mindie_exec.process_identity(later.pid)
                if second:
                    break
                time.sleep(0.02)
            self.assertIsInstance(second, dict)
            self.assertNotEqual(second, first)
            self.assertIn(marker, second["command"])
        finally:
            later.kill()
            later.wait(timeout=3)

    def test_owned_process_kills_grandchild_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pids = Path(tmp) / "pids"
            with self.assertRaisesRegex(RuntimeError, "startup rejected"):
                with mindie_exec.owned_process(
                    [sys.executable, "-c", SLEEP_CHILD, tmp],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ) as proc:
                    deadline = time.monotonic() + 5
                    while not pids.is_file() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    parent, child = (int(part) for part in pids.read_text().split())
                    self.assertEqual(proc.pid, parent)
                    self.assertTrue(mindie_exec.pid_alive(parent))
                    self.assertTrue(mindie_exec.pid_alive(child))
                    raise RuntimeError("startup rejected")
            self.assertTrue(_wait_dead(parent))
            self.assertTrue(_wait_dead(child))

    def test_detach_requires_verifiable_identity_and_leaves_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pids = Path(tmp) / "pids"
            with mindie_exec.owned_process(
                [sys.executable, "-c", SLEEP_CHILD, tmp],
                detach_on_success=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) as proc:
                deadline = time.monotonic() + 5
                while not pids.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                parent, child = (int(part) for part in pids.read_text().split())
                identity = mindie_exec.process_identity(proc.pid)
                self.assertTrue(identity and identity["started"] and identity["command"])
            try:
                self.assertTrue(mindie_exec.pid_alive(parent))
                self.assertTrue(mindie_exec.pid_alive(child))
                self.assertEqual(mindie_exec.process_identity(parent), identity)
            finally:
                if os.name != "nt":
                    try:
                        os.killpg(parent, 9)
                    except OSError:
                        pass
                try:
                    os.kill(parent, 9)
                except OSError:
                    pass
                try:
                    os.kill(child, 9)
                except OSError:
                    pass
                self.assertTrue(_wait_dead(parent))
                self.assertTrue(_wait_dead(child))

    def test_detach_without_identity_cleans_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pids = Path(tmp) / "pids"
            original = mindie_exec.process_identity

            def hidden(pid):
                return None

            mindie_exec.process_identity = hidden  # type: ignore[method-assign]
            try:
                with mindie_exec.owned_process(
                    [sys.executable, "-c", SLEEP_CHILD, tmp],
                    detach_on_success=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ):
                    deadline = time.monotonic() + 5
                    while not pids.is_file() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    parent, child = (int(part) for part in pids.read_text().split())
            finally:
                mindie_exec.process_identity = original  # type: ignore[method-assign]
            self.assertTrue(_wait_dead(parent))
            self.assertTrue(_wait_dead(child))

    def test_pid_alive_rejects_non_positive(self) -> None:
        self.assertFalse(mindie_exec.pid_alive(0))
        self.assertFalse(mindie_exec.pid_alive(-1))
        self.assertIsNone(mindie_exec.process_identity(0))


if __name__ == "__main__":
    unittest.main()
