"""Real process output and failure survive independently injected cleanup faults."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import bounded_process
import mcp_gate
import runtime_call


class ProcessResultTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'actual POSIX subprocess evidence')
    def test_confirmed_output_survives_cleanup_failure(self):
        process = bounded_process._spawn(
            [sys.executable, '-c', 'print(\'{"content":[],"structuredContent":{"receipt":"done"},"isError":false}\')'],
            subprocess.DEVNULL, None)
        original = process.wait
        def wait(*args, **kwargs):
            if kwargs.get('timeout') == 1:
                raise subprocess.TimeoutExpired('cleanup fixture', 1)
            return original(*args, **kwargs)
        with patch.object(process, 'wait', side_effect=wait):
            result = bounded_process._run_posix(process, None, 4096, None)
        self.assertEqual(result.execution, 'completed')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.cleanup, [{'stage': 'reap_process', 'error_type': 'TimeoutExpired'}])
        with self.assertRaises(bounded_process.ProcessCleanupError) as caught:
            result.checked_stdout()
        self.assertIs(caught.exception.process_result, result)
        shaped = mcp_gate.runtime_result(result)
        self.assertEqual(shaped['structuredContent']['receipt'], 'done')
        self.assertEqual(shaped['cleanup']['operation_outcome'], 'preserved')
        self.assertTrue(shaped['isError'])

    @unittest.skipUnless(os.name == 'posix', 'actual POSIX subprocess evidence')
    def test_primary_deadline_survives_cleanup_failure(self):
        process = bounded_process._spawn([sys.executable, '-c', 'import time;time.sleep(3)'],
                                        subprocess.DEVNULL, None)
        with patch.object(process, 'wait', side_effect=OSError('cleanup failure')):
            with self.assertRaises(TimeoutError) as caught:
                bounded_process._run_posix(process, .1, 4096, None)
        process.wait(timeout=3)
        result = caught.exception.process_result
        self.assertEqual(result.execution, 'unknown')
        self.assertIn({'stage': 'reap_process', 'error_type': 'OSError'}, result.cleanup)

    def test_selected_missing_generation_cannot_run_old_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'adapter.json'
            config.write_text('{"python":' + __import__('json').dumps(sys.executable)
                              + ',"runtime_scripts":' + __import__('json').dumps(directory) + '}')
            with patch.object(runtime_call, 'config_path', return_value=config):
                with self.assertRaises(FileNotFoundError):
                    runtime_call.redispatch()

    def test_nonexistent_executable_is_not_started(self):
        with self.assertRaises(OSError) as caught:
            bounded_process.run(['/definitely-absent/mindie-executable'], '')
        self.assertEqual(caught.exception.process_result.execution, 'not_started')

    @unittest.skipUnless(os.name == 'posix', 'POSIX startup receipt')
    def test_lost_start_receipt_after_effect_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'owned_process.py').write_text('import subprocess,sys;subprocess.run(sys.argv[4:],check=True)')
            marker = root / 'effect'
            command = f'from pathlib import Path;Path({str(marker)!r}).write_text("once")'
            with patch.object(bounded_process, '__file__', str(root / 'bounded_process.py')):
                with self.assertRaises(OSError) as caught:
                    bounded_process.run([sys.executable, '-c', command], '')
            self.assertEqual(marker.read_text(), 'once')
            self.assertEqual(caught.exception.process_result.execution, 'unknown')


if __name__ == '__main__':
    unittest.main()
