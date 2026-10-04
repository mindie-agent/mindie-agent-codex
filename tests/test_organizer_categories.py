"""K3-derived failure dimensions across the real worker protocol boundary.

These are controlled native-process faults on anonymous case inputs, not copied
history or claims that a K3 task experienced every injected failure.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import agent_worker as worker
import process_guard as guard
from mindie_knowledge.materials import summarizer
from k3_material_fixture import request, completion


def process_with_events(events, wait=False):
    program = 'import time\n' + '\n'.join(f'print({json.dumps(event)!r}, flush=True)' for event in events)
    if wait:
        program += '\ntime.sleep(10)'
    return [sys.executable, '-c', program]


class OrganizerCategoryTests(unittest.TestCase):
    def test_k3_01_known_model_rejection_is_not_unknown_paid_outcome(self):
        payload = request(worker)
        rejection = "HTTP 400: This model is not supported when using Codex with a ChatGPT account"
        events = [dict(type='error', message=rejection), dict(type='turn.failed', error=dict(message=rejection))]
        calls = []
        def native(command, prompt, **options):
            calls.append(command)
            return guard.run_codex(process_with_events(events), prompt, **options)
        with patch.object(worker, 'run_codex', native):
            value = worker.run(payload)
        self.assertEqual(len(calls), 1)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'configuration')
        self.assertEqual(value['billing_status'], 'rejected')
        self.assertEqual(value['error_reason'], 'native_model_rejected')
        self.assertEqual(value['model_calls'], 1)
        self.assertIsNone(value['usage'])
        self.assertFalse(value['usage_known'])
        self.assertNotIn(rejection, json.dumps(value))
        summarizer.validate_outcome(value, payload)

    def test_k3_04_terminal_failure_is_known_but_unreported_usage_stays_unknown(self):
        payload = request(worker, 'K3-04')
        def native(command, prompt, **options):
            return guard.run_codex(process_with_events([dict(type='turn.failed', error=dict(message='SYNTHETIC_FAILURE'))]), prompt, **options)
        with patch.object(worker, 'run_codex', native):
            value = worker.run(payload)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'native')
        self.assertEqual(value['billing_status'], 'unknown')
        self.assertIsNone(value['usage'])

    def test_k3_03_tool_attempt_aborts_without_waiting_for_more_progress(self):
        payload = request(worker, 'K3-03')
        def native(command, prompt, **options):
            return guard.run_codex(process_with_events([
                dict(type='item.started', item=dict(type='command_execution'))], wait=True), prompt, **options)
        start = time.monotonic()
        with patch.object(worker, 'run_codex', native):
            value = worker.run(payload)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(value['status'], 'outcome_unknown')
        self.assertEqual(value['error'], 'native')
        self.assertEqual(value['model_calls'], 1)

    def test_k3_02_invalid_return_keeps_completed_usage_without_repeating(self):
        payload = request(worker, 'K3-02')
        def native(command, prompt, **options):
            completion(options['receipt'])
            Path(command[command.index('--output-last-message') + 1]).write_text('not-json')
        with patch.object(worker, 'run_codex', native):
            value = worker.run(payload)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'invalid_result')
        self.assertEqual(value['raw_result'], 'not-json')
        self.assertEqual(value['error_reason'], 'result_json_invalid')
        self.assertEqual(value['usage']['input_tokens'], 120)
        self.assertEqual(value['model_calls'], 1)

    def test_k3_04_lost_native_stream_never_returns_empty_success(self):
        payload = request(worker, 'K3-04')
        def native(command, prompt, **options):
            return guard.run_codex([sys.executable, '-c', 'pass'], prompt, **options)
        with patch.object(worker, 'run_codex', native):
            value = worker.run(payload)
        self.assertEqual(value['status'], 'outcome_unknown')
        self.assertEqual(value['error'], 'native')
        self.assertIsNone(value['result'])

    def test_k3_01_cli_configuration_failure_is_explicit_envelope(self):
        with patch.dict(os.environ, MINDIE_CODEX_BIN='/missing/k3-small-model'):
            payload = request(worker)
            completed = subprocess.run([sys.executable, str(SCRIPTS / 'agent_worker.py')],
                                       input=json.dumps(payload), text=True, capture_output=True,
                                       timeout=5, env=dict(os.environ))
        self.assertEqual(completed.returncode, 0)
        value = json.loads(completed.stdout)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'configuration')
        self.assertEqual(value['model_calls'], 0)
        self.assertNotIn('No such file', completed.stderr)

    def test_k3_03_cli_rejects_raw_history_sized_wire_before_a_model(self):
        completed = subprocess.run([sys.executable, str(SCRIPTS / 'agent_worker.py')],
                                   input='x' * (worker.MAX_REQUEST_BYTES + 1), text=True,
                                   capture_output=True, timeout=3,
                                   env=dict(os.environ, MINDIE_CODEX_BIN='/missing/k3-not-called'))
        self.assertEqual(completed.returncode, 65)
        self.assertEqual(completed.stderr.strip(), 'summary protocol failed: invalid_result')
        self.assertEqual(completed.stdout, '')

    def test_k3_04_unexpected_failure_does_not_expose_source_text(self):
        payload = request(worker, 'K3-04')
        with patch.object(worker, 'run_codex', side_effect=RuntimeError('PRIVATE_CASE_MARKER')):
            value = worker.run(payload)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'unknown')
        self.assertNotIn('PRIVATE_CASE_MARKER', json.dumps(value))


if __name__ == '__main__':
    unittest.main()
