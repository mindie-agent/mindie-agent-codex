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
from k3_material_fixture import request, result, isolate_native_profile
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
    def setUp(self):
        isolate_native_profile(self)

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
        actual_wait = subprocess.Popen.wait
        def fast_reap_wait(process, *args, **kwargs):
            if kwargs.get('timeout') in (2, 2.0, 10, 10.0):
                kwargs = dict(kwargs, timeout=0.1)
            return actual_wait(process, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            late = Path(directory) / 'late-output.json'
            with patch.object(subprocess.Popen, 'wait', fast_reap_wait):
                value = self.execute(payload, events(metadata),
                    f'time.sleep(0.5)\nfrom pathlib import Path\nPath({str(late)!r}).write_text("too late")\n')
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
        self.assertEqual(value['error_reason'], 'native_extra_completion')

    def test_native_failure_observations_reach_the_caller_without_private_events(self):
        cases = [
            (dict(type='error', message='Reconnecting... 1/5 PRIVATE_NATIVE_CANARY'),
             'native_reconnecting', 'outcome_unknown'),
            (dict(type='error', error=dict(message='stream disconnected PRIVATE_NATIVE_CANARY')),
             'native_stream_disconnected', 'outcome_unknown'),
            (dict(type='error', message='PRIVATE_NATIVE_CANARY'),
             'native_event_error', 'outcome_unknown'),
            (dict(type='turn.failed', error=dict(message='PRIVATE_NATIVE_CANARY')),
             'native_turn_failed', 'failed'),
            (dict(type='item.completed', item=dict(type='command_execution', command='PRIVATE_NATIVE_CANARY')),
             'native_forbidden_item', 'outcome_unknown'),
            (dict(type='turn.started'), 'native_extra_turn', 'outcome_unknown'),
        ]
        for event, reason, status in cases:
            with self.subTest(reason=reason):
                payload = request(worker)
                value = self.execute(payload, [dict(type='turn.started'), event])
                self.assertEqual(value['status'], status)
                self.assertEqual(value['error_reason'], reason)
                self.assertEqual(value['model_calls'], 1)
                self.assertIsNone(value['result'])
                self.assertIsNone(value['usage'])
                self.assertNotIn('PRIVATE_NATIVE_CANARY', json.dumps(value))
                self.assertNotIn('PRIVATE_NATIVE_CANARY', json.dumps(summarizer.failure_detail(value)))
                summarizer.validate_outcome(value, payload)

    def test_unsuccessful_cli_exit_has_a_distinct_uncertain_result(self):
        payload = request(worker)
        value = self.execute(payload, [dict(type='turn.started')], 'sys.exit(7)\n')
        self.assertEqual(value['status'], 'outcome_unknown')
        self.assertEqual(value['error_reason'], 'native_exit_nonzero')
        self.assertEqual(value['billing_status'], 'unknown')
        self.assertIsNone(value['result'])

    def test_untrusted_native_reason_cannot_become_a_public_diagnostic(self):
        for reason in ['PRIVATE_NATIVE_CANARY', 'block_identity_mismatch', ['PRIVATE_NATIVE_CANARY']]:
            with self.subTest(reason=reason):
                payload = request(worker)
                def fail(_command, prompt, **options):
                    options['receipt']['native_started'] = True
                    raise guard.NativeFailure('PRIVATE_NATIVE_CANARY', reason=reason)
                with patch.object(worker, 'run_codex', fail):
                    value = worker.run(payload)
                self.assertEqual(value['status'], 'outcome_unknown')
                self.assertEqual(value['error_reason'], 'native_failure')
                self.assertNotIn('PRIVATE_NATIVE_CANARY', json.dumps(value))

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
            profile_record = root / 'profile'
            handshake = root / 'result-saved'
            with patch.dict(os.environ, MINDIE_CODEX_BIN=str(fake)):
                payload = request(worker)
                metadata = result(payload)
                writer = (
                    "import time;from pathlib import Path\n"
                    f"ready=Path({str(handshake)!r})\n"
                    "Path('writer-ready').touch()\n"
                    "while not ready.exists(): time.sleep(.01)\n"
                    "Path('writer-after-result').touch()\n"
                    "time.sleep(30)\n"
                )
                setup = (
                    "import os,sys,subprocess,time;from pathlib import Path\n"
                    "assert 'MINDIE_SUMMARY_SCRATCH' not in os.environ\n"
                    "profile=Path(sys.argv[sys.argv.index('-C')+1]);os.chdir(profile)\n"
                    f"Path({str(profile_record)!r}).write_text(str(profile))\n"
                    f"subprocess.Popen([sys.executable,'-c',{writer!r}])\n"
                    "while not Path('writer-ready').exists(): time.sleep(.01)\n"
                )
                fake.write_text('#!' + sys.executable + '\n' + setup
                                + program(events(metadata), 'time.sleep(30)\n'), encoding='utf-8')
                fake.chmod(0o700)
                saved = []
                cleanup = {}
                def record(raw):
                    profile = Path(profile_record.read_text())
                    self.assertTrue((profile / 'schema.json').is_file())
                    saved.append(json.loads(raw))
                    handshake.touch()
                    deadline = time.monotonic() + 2
                    while not (profile / 'writer-after-result').exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue((profile / 'writer-after-result').exists())
                started = time.monotonic()
                raw = bounded_run([sys.executable, str(SCRIPTS / 'agent_worker.py')], json.dumps(payload),
                                  timeout=10, max_output=summarizer.MAX_RESPONSE_BYTES * 3,
                                  on_result=record, cleanup_receipt=cleanup)
                value = json.loads(raw)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(saved[0]['result'], metadata)
            self.assertEqual(value['status'], 'returned')
            self.assertFalse(value['cleanup_failed'])
            self.assertFalse(cleanup.get('cleanup_failed', False))
            self.assertFalse(Path(profile_record.read_text()).parent.exists())
            summarizer.validate_outcome(value, payload)


if __name__ == '__main__':
    unittest.main()
