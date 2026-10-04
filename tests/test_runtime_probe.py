"""Setup and updater reject failed processes even when stdout says OK."""

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"))
import auto_update
from bounded_process import run

FAULT = "print('OK', flush=True)\nraise RuntimeError('controlled prior API failure')\n"


class RuntimeProbeTests(unittest.TestCase):
    def test_exit1_after_ok_is_rejected_before_install(self):
        direct = subprocess.run([sys.executable, "-c", FAULT], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(direct.returncode, 1)
        self.assertEqual(direct.stdout, "OK\n")
        with self.assertRaisesRegex(RuntimeError, "MindIE runtime failed; not retried"):
            run([sys.executable, "-c", FAULT], "", timeout=15)

        def command(argv, **kwargs):
            argv = [str(arg) for arg in argv]
            argv[2] = FAULT + argv[2]
            return run(argv, "", **kwargs)

        with self.assertRaisesRegex(RuntimeError, "MindIE runtime failed; not retried"):
            auto_update.Updater.probe_runtime(SimpleNamespace(command=command), sys.executable)


if __name__ == "__main__":
    unittest.main()
