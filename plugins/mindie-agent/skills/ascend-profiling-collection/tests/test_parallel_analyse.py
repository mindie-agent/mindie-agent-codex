"""Exercise generated container bash locally with stub rank payloads.

Collected by the existing workspace Skill test discovery on every platform.
Windows has no native POSIX process/path semantics. macOS can run the parallel
and archive cases without GNU timeout; real timeout coverage is a separate test.
"""

from __future__ import annotations

import os
import subprocess
import sys

from pathlib import Path
for _p in Path(__file__).resolve().parents:
    if (_p / "domain-lib").is_dir():
        if str(_p / "domain-lib") not in sys.path:
            sys.path.insert(0, str(_p / "domain-lib"))
        break
else:
    raise RuntimeError("MindIE domain-lib not found; use the installed plugin")
import tempfile
import time
import unittest
from pathlib import Path


CHECKS = Path(__file__).with_name("parallel_analyse_checks.py")


@unittest.skipUnless(os.name == "posix", "requires native POSIX bash/process semantics; Windows timeout.exe is not GNU timeout")
class ParallelAnalyseIntegrationTests(unittest.TestCase):
    def run_checks(self, *arguments: str) -> None:
        # Isolate the old self-test's sibling imports from other Skill suites.
        from remote_dev.core.local_process import OwnedProcess

        with tempfile.TemporaryDirectory(prefix="parallel_analyse_test_") as workspace, tempfile.TemporaryFile() as log:
            with OwnedProcess([sys.executable, str(CHECKS), *arguments],
                              stdout=log, stderr=subprocess.STDOUT,
                              env={**os.environ, "TMPDIR": workspace}) as owner:
                process = owner.process
                deadline = time.monotonic() + 45
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("parallel analyse self-test exceeded 45 seconds")
                    if os.fstat(log.fileno()).st_size > 512 * 1024:
                        self.fail("parallel analyse self-test output exceeded 512 KiB")
                    time.sleep(0.01)
            log.seek(0)
            raw = log.read(512 * 1024 + 1)
            self.assertLessEqual(len(raw), 512 * 1024, "self-test output exceeded 512 KiB")
            output = raw.decode("utf-8", errors="replace")
        if process.returncode == 77:
            self.skipTest(output.strip())
        self.assertEqual(process.returncode, 0, output)

    def test_parallel_logs_results_modes_and_archive(self) -> None:
        self.run_checks()

    def test_gnu_timeout_terminates_stuck_rank(self) -> None:
        self.run_checks("--timeout-only")


if __name__ == "__main__":
    unittest.main()
