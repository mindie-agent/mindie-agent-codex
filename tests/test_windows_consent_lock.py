"""Native Windows regression for contention on an empty byte-range lock."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


@unittest.skipUnless(os.name == "nt", "MSVCRT byte-range lock contract")
class WindowsConsentLockTests(unittest.TestCase):
    def test_empty_lock_contention_is_bounded_and_maps_to_consent_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "mindie-consent.json.lock"
            ready = root / "holder-ready"
            release = root / "holder-release"
            holder_script = root / "holder.py"
            holder_script.write_text(
                "import msvcrt, os, sys, time\n"
                "from pathlib import Path\n"
                "lock, ready, release = map(Path, sys.argv[1:])\n"
                "fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)\n"
                "os.lseek(fd, 0, os.SEEK_SET)\n"
                "msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
                "ready.write_text('locked')\n"
                "deadline = time.monotonic() + 8\n"
                "while not release.exists() and time.monotonic() < deadline:\n"
                " time.sleep(0.01)\n"
                "msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)\n"
                "os.close(fd)\n",
                encoding="utf-8",
            )
            contender_script = root / "contender.py"
            contender_script.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
                "import consent_store\n"
                "consent_store.LOCK_POLL_SECONDS = 0.02\n"
                "try:\n"
                " with consent_store._UpdateLock(sys.argv[1], wait=0.4):\n"
                "  raise AssertionError('contended lock was acquired')\n"
                "except consent_store.ConsentError as exc:\n"
                " if exc.state != 'locked': raise\n"
                " print('locked')\n",
                encoding="utf-8",
            )
            holder = subprocess.Popen(
                [sys.executable, str(holder_script), str(lock_path), str(ready), str(release)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            contender = None
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not ready.exists():
                    if holder.poll() is None:
                        holder.kill()
                    stdout, stderr = holder.communicate(timeout=2)
                    self.fail(
                        "empty-byte lock holder failed: "
                        + repr((stdout, stderr, holder.returncode))
                    )
                self.assertEqual(lock_path.stat().st_size, 0)
                contender = subprocess.Popen(
                    [sys.executable, str(contender_script), str(lock_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, stderr = contender.communicate(timeout=4)
                self.assertEqual(
                    contender.returncode,
                    0,
                    stderr.decode("utf-8", "replace"),
                )
                self.assertEqual(stdout.decode("utf-8", "replace").strip(), "locked")
            finally:
                release.write_text("release")
                for process in (contender, holder):
                    if process is None:
                        continue
                    if process.poll() is None:
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
