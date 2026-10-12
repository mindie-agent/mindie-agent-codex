"""A completed native answer is independent of CLI files and process cleanup.

Real local process faults, anonymous fixture text, zero provider calls.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import agent_worker as worker
import process_guard as guard
from k3_material_fixture import request, result
from mindie_knowledge.loop.process import bounded_run
from mindie_knowledge.materials import summarizer


def events(metadata, *, terminal=True):
    rows = [dict(type='turn.started'),
            dict(type='item.completed', item=dict(type='reasoning', text='PRIVATE_REASONING_CANARY')),
            dict(type='item.completed', item=dict(type='agent_message', phase='commentary', text='Commentary is not metadata.')),
            dict(type='item.completed', item=dict(type='agent_message', phase='final_answer', text=json.dumps(metadata)))]
    if terminal:
        rows.append(dict(type='turn.completed', usage=dict(input_tokens=120, cached_input_tokens=20, output_tokens=30)))
    return rows


def program(rows, tail=''):
    return 'import json,sys,time\n' + '\n'.join(
        f'print({json.dumps(row)!r},flush=True)' for row in rows) + '\n' + tail


class NativeSummaryResultTests(unittest.TestCase):
    def execute(self, payload, rows, tail=''):
        # Large output must exercise the result stream, not Windows' much
        # smaller command-line limit before the fake native process starts.
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'native.py'
            script.write_text(program(rows, tail), encoding='utf-8')
            command = [sys.executable, str(script)]
            def native(_command, prompt, **options):
                return guard.run_codex(command, prompt, **options)
            with patch.object(worker, 'run_codex', native):
                return worker.run(payload)

    def test_completed_result_survives_cli_that_never_writes_its_late_file(self):
        payload = request(worker)
        metadata = result(payload)
        with tempfile.TemporaryDirectory() as directory:
            late = Path(directory) / 'late-output.json'
            value = self.execute(payload, events(metadata),
                f'time.sleep(3)\nfrom pathlib import Path\nPath({str(late)!r}).write_text("too late")\n')
            self.assertFalse(late.exists())
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result'], metadata)
        self.assertEqual(json.loads(value['raw_result']), metadata)
        self.assertEqual(value['usage']['input_tokens'], 120)
        self.assertEqual(value['model_calls'], 1)
        self.assertTrue(value['cleanup_failed'])
        self.assertNotIn('PRIVATE_REASONING_CANARY', json.dumps(value))
        summarizer.validate_outcome(value, payload)

    def test_completed_turn_without_completed_answer_is_visible_known_failure(self):
        payload = request(worker)
        rows = events(result(payload))
        rows[-2]['type'] = 'item.started'
        value = self.execute(payload, rows)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error_reason'], 'native_output_unreadable')
        self.assertTrue(value['usage_known'])
        self.assertIsNone(value['raw_result'])
        self.assertNotIn('PRIVATE_REASONING_CANARY', json.dumps(value))

    def test_answer_without_terminal_evidence_is_not_applied(self):
        payload = request(worker)
        value = self.execute(payload, events(result(payload), terminal=False))
        self.assertEqual(value['status'], 'outcome_unknown')
        self.assertEqual(value['model_calls'], 1)
        self.assertIsNone(value['result'])
        self.assertIsNone(value['usage'])

    def test_last_completed_answer_is_the_result_without_phase_metadata(self):
        payload = request(worker)
        metadata = result(payload)
        rows = events(metadata)
        rows.insert(1, dict(type='item.completed', item=dict(type='agent_message', text='earlier answer')))
        del rows[-2]['item']['phase']
        value = self.execute(payload, rows)
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result'], metadata)

    def test_second_completion_is_rejected_instead_of_overwriting_usage(self):
        payload = request(worker)
        rows = events(result(payload))
        rows.append(dict(type='turn.completed', usage=dict(input_tokens=999999)))
        value = self.execute(payload, rows)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['usage']['input_tokens'], 120)
        self.assertEqual(value['error'], 'native')

    def test_reaping_failure_preserves_known_answer_and_usage(self):
        payload = request(worker)
        actual_wait = subprocess.Popen.wait
        def wait(process, *args, **kwargs):
            if kwargs.get('timeout') == 2:
                raise OSError('PRIVATE_REAP_CANARY')
            return actual_wait(process, *args, **kwargs)
        with patch.object(subprocess.Popen, 'wait', wait):
            value = self.execute(payload, events(result(payload)))
        self.assertEqual(value['status'], 'returned')
        self.assertTrue(value['cleanup_failed'])
        self.assertEqual(value['usage']['input_tokens'], 120)
        self.assertNotIn('PRIVATE_REAP_CANARY', json.dumps(value))

    def test_oversized_completed_metadata_keeps_known_usage(self):
        payload = request(worker)
        metadata = result(payload)
        metadata['navigation']['summary'] = 'x' * (summarizer.MAX_RESPONSE_BYTES + 1)
        value = self.execute(payload, events(metadata))
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'output_limit')
        self.assertEqual(value['model_calls'], 1)
        self.assertTrue(value['usage_known'])

    @unittest.skipUnless(os.name == 'posix', 'owned native POSIX process tree')
    def test_core_records_answer_before_reaping_native_and_its_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'fake-codex'
            marker = root / 'descendant-survived'
            with patch.dict(os.environ, MINDIE_CODEX_BIN=str(fake)):
                payload = request(worker)
                metadata = result(payload)
                tail = ('import subprocess\n' +
                    f'subprocess.Popen([sys.executable,"-c",{("import time;from pathlib import Path;time.sleep(3);Path(" + repr(str(marker)) + ").touch()")!r}])\n' +
                    'time.sleep(30)\n')
                fake.write_text('#!' + sys.executable + '\n' + program(events(metadata), tail))
                fake.chmod(0o700)
                saved = []
                cleanup = {}
                started = time.monotonic()
                raw = bounded_run([sys.executable, str(SCRIPTS / 'agent_worker.py')], json.dumps(payload),
                                  timeout=10, max_output=summarizer.MAX_RESPONSE_BYTES * 3,
                                  on_result=lambda raw: saved.append(json.loads(raw)), cleanup_receipt=cleanup)
                value = json.loads(raw)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(saved[0]['result'], metadata)
            self.assertEqual(value['status'], 'returned')
            self.assertFalse(value['cleanup_failed'])
            self.assertFalse(cleanup.get('cleanup_failed', False))
            time.sleep(3.1)
            self.assertFalse(marker.exists())
            summarizer.validate_outcome(value, payload)


if __name__ == '__main__':
    unittest.main()
