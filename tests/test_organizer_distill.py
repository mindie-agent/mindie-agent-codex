"""Explicit C6 organizer branch over the real shared distillation contract.

The joint 0.0.3 knowledge source supplies `mindie_knowledge.graded.distill`.
No test starts real Codex or a provider call; local subprocess doubles only.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import agent_worker
from mindie_knowledge.materials import summarizer
from k3_material_fixture import request, result, completion, isolate_native_profile

try:
    from mindie_knowledge.graded import distill
except ImportError:  # installed 0.0.2 runtime has no C6 surface
    distill = None

SESSION = 'a' * 64


@unittest.skipIf(distill is None, 'joint 0.0.3 knowledge source is required')
class DistillOrganizerTests(unittest.TestCase):
    def setUp(self):
        isolate_native_profile(self)

    def prepared(self, task_state='verified_milestone', *, outcome='verified',
                 kind='hardware_spec', content='Synthetic hardware specification.'):
        evidence = [dict(kind=kind, outcome=outcome, source_locator='fixture://case',
                         content=content, evidence_refs=['ref-1'])]
        identity = agent_worker.identity()
        with patch('mindie_knowledge.loop.transcript_redaction.redact_tree',
                   side_effect=lambda text, **options: (text, [])):
            base = distill.prepare_request(
                evidence, session_digest=SESSION, task_state=task_state,
                redactor_executable='/bin/true', redaction_key='synthetic-key')
        self.assertIn('mindie-distill-request/1', identity['capabilities'])
        return distill.with_worker_identity(base, identity)

    def decision(self, prepared):
        item = prepared['sanitized_inputs'][0]
        frontmatter = {
            'schema': 'mindie-graded-unit/1',
            'id': 'unit-fixture-hardware-axiom',
            'tier': 'L1',
            'category': 'axiom',
            'evidence_strength': 'hardware_spec',
            'domain_path': 'hardware/ascend',
            'inherits_from': [],
            'title': 'Synthetic hardware axiom',
            'triggers': {'error_signatures': ['SYNTHETIC'], 'keywords': ['synthetic']},
            'conditions': {'soc': ['Ascend910B']},
            'provenance': {'source_type': 'hardware_spec',
                           'source_locator': item['source_locator'],
                           'source_digest': item['source_digest'],
                           'evidence_refs': ['ref-1'], 'supersedes': []},
        }
        return {
            'schema_version': 'mindie-distill-decision/1',
            'session_digest': prepared['session_digest'],
            'evidence_inputs': prepared['evidence_inputs'],
            'action': 'propose_graded_units',
            'drop_reason': None,
            'units': [{
                'target_tier': 'L1', 'category': 'axiom',
                'evidence_strength': 'hardware_spec', 'domain_path': 'hardware/ascend',
                'operation': 'create', 'target_unit_id': 'unit-fixture-hardware-axiom',
                'frontmatter': frontmatter, 'markdown_body': 'Synthetic rule body.\n',
            }],
        }

    def native(self, payload):
        def call(command, prompt, **options):
            options['receipt'].update(
                native_started=True, turn_started=True, turn_completed=True,
                usage=dict(input_tokens=120, cached_input_tokens=20, output_tokens=30),
                final_message=json.dumps(payload))
        return call

    def test_explicit_c6_request_returns_validated_decision_with_usage(self):
        prepared = self.prepared()
        expected = self.decision(prepared)
        with patch.object(agent_worker, 'run_codex', self.native(expected)):
            value = agent_worker.run(prepared)
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result'], expected)
        self.assertEqual(value['model_calls'], 1)
        self.assertEqual(value['usage'], dict(input_tokens=120, cached_input_tokens=20,
                                              output_tokens=30))
        self.assertTrue(value['usage_known'])
        self.assertEqual(value['billing_status'], 'reported')
        self.assertIsNone(value['error'])
        summarizer.validate_outcome(value, prepared)

    def test_in_flight_stop_returns_shared_noop_without_paid_call(self):
        prepared = self.prepared('in_flight')
        with patch.object(agent_worker, 'run_codex') as native:
            value = agent_worker.run(prepared)
        native.assert_not_called()
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result']['action'], 'noop')
        self.assertEqual(value['result']['drop_reason'], 'in_flight_incomplete')
        self.assertEqual(value['model_calls'], 0)
        self.assertEqual(value['billing_status'], 'not_called')
        self.assertIsNone(value['usage'])
        summarizer.validate_outcome(value, prepared)

    def test_pre_send_failure_is_known_and_uncalled(self):
        prepared = self.prepared()
        def missing(command, prompt, **options):
            raise agent_worker.NativeStartError('synthetic missing executable')
        with patch.object(agent_worker, 'run_codex', missing):
            value = agent_worker.run(prepared)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'configuration')
        self.assertEqual(value['error_reason'], 'native_start_failed')
        self.assertEqual(value['model_calls'], 0)
        self.assertEqual(value['billing_status'], 'not_called')
        self.assertIsNone(value['result'])
        summarizer.validate_outcome(value, prepared)

    def test_post_send_interruption_is_unknown_and_not_replayed(self):
        prepared = self.prepared()
        calls = []
        def interrupted(command, prompt, **options):
            calls.append(1)
            options['receipt'].update(native_started=True, turn_started=True)
            raise TimeoutError('private provider detail must not escape')
        with patch.object(agent_worker, 'run_codex', interrupted):
            value = agent_worker.run(prepared)
        self.assertEqual(len(calls), 1)
        self.assertEqual(value['status'], 'outcome_unknown')
        self.assertEqual(value['error'], 'deadline')
        self.assertEqual(value['error_reason'], 'native_deadline')
        self.assertEqual(value['model_calls'], 1)
        self.assertEqual(value['billing_status'], 'unknown')
        self.assertIsNone(value['usage'])
        self.assertNotIn('private provider', json.dumps(value))
        summarizer.validate_outcome(value, prepared)

    def test_invented_evidence_reference_is_rejected_with_usage_retained(self):
        prepared = self.prepared()
        invalid = self.decision(prepared)
        invalid['units'][0]['frontmatter']['provenance']['evidence_refs'] = ['invented-ref']
        with patch.object(agent_worker, 'run_codex', self.native(invalid)):
            value = agent_worker.run(prepared)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'invalid_result')
        self.assertEqual(value['error_reason'], 'result_contract_invalid')
        self.assertEqual(value['model_calls'], 1)
        self.assertTrue(value['usage_known'])
        self.assertIsNone(value['result'])
        self.assertEqual(json.loads(value['raw_result']), invalid)
        summarizer.validate_outcome(value, prepared)

    def test_invalid_json_is_a_distinct_contract_failure(self):
        prepared = self.prepared()
        def not_json(command, prompt, **options):
            options['receipt'].update(
                native_started=True, turn_started=True, turn_completed=True,
                usage=dict(input_tokens=10, cached_input_tokens=0, output_tokens=5),
                final_message='not-json')
        with patch.object(agent_worker, 'run_codex', not_json):
            value = agent_worker.run(prepared)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error_reason'], 'result_json_invalid')
        self.assertEqual(value['raw_result'], 'not-json')
        self.assertTrue(value['usage_known'])
        summarizer.validate_outcome(value, prepared)

    def test_missing_shared_c6_surface_fails_before_model(self):
        prepared = self.prepared()
        with patch.object(agent_worker, 'identity', return_value=prepared['policy_identity']), \
                patch.object(distill, 'build_prompt', None), \
                patch.object(agent_worker, 'run_codex') as native:
            value = agent_worker.run(prepared)
        native.assert_not_called()
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'configuration')
        self.assertEqual(value['error_reason'], 'dependency_unavailable')
        self.assertEqual(value['model_calls'], 0)
        self.assertIsNone(value['result'])
        summarizer.validate_outcome(value, prepared)

    def test_shared_prompt_and_schema_are_the_only_contract_source(self):
        prepared = self.prepared()
        expected = self.decision(prepared)
        seen = []
        def build_prompt(request):
            seen.append(request)
            return 'C6-PROMPT-SENTINEL'
        def native(command, prompt, **options):
            self.assertEqual(prompt, 'C6-PROMPT-SENTINEL')
            schema_path = Path(command[command.index('--output-schema') + 1])
            self.assertEqual(json.loads(schema_path.read_text()),
                             {'type': 'object', 'title': 'sentinel'})
            options['receipt'].update(
                native_started=True, turn_started=True, turn_completed=True,
                usage=dict(input_tokens=1, cached_input_tokens=0, output_tokens=1),
                final_message=json.dumps(expected))
        with patch.object(distill, 'build_prompt', build_prompt), \
                patch.object(distill, 'decision_schema',
                             return_value={'type': 'object', 'title': 'sentinel'}), \
                patch.object(agent_worker, 'run_codex', native):
            value = agent_worker.run(prepared)
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['sanitized_inputs'][0]['content'],
                         'Synthetic hardware specification.')
        summarizer.validate_outcome(value, prepared)

    def test_local_cleanup_bookkeeping_does_not_invert_returned_decision(self):
        prepared = self.prepared()
        expected = self.decision(prepared)
        with patch.object(agent_worker, 'run_codex', self.native(expected)), \
                patch.object(agent_worker.shutil, 'rmtree',
                             side_effect=OSError('synthetic cleanup failure')):
            value = agent_worker.run(prepared)
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result'], expected)
        self.assertTrue(value['cleanup_failed'])
        self.assertEqual(value['usage']['input_tokens'], 120)
        summarizer.validate_outcome(value, prepared)

    def test_cli_returns_c6_decision_over_the_process_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'fake-codex'
            with patch.dict(os.environ, MINDIE_CODEX_BIN=str(fake)):
                prepared = self.prepared()
                expected = self.decision(prepared)
                rows = [
                    {'type': 'turn.started'},
                    {'type': 'item.completed',
                     'item': {'type': 'agent_message', 'phase': 'final_answer',
                              'text': json.dumps(expected)}},
                    {'type': 'turn.completed',
                     'usage': {'input_tokens': 120, 'cached_input_tokens': 20,
                               'output_tokens': 30}},
                ]
                fake.write_text('#!' + sys.executable + '\n' + '\n'.join(
                    f'print({json.dumps(row)!r}, flush=True)' for row in rows), encoding='utf-8')
                fake.chmod(0o700)
                completed = subprocess.run(
                    [sys.executable, str(SCRIPTS / 'agent_worker.py')],
                    input=json.dumps(prepared), text=True, capture_output=True, timeout=15,
                    env=dict(os.environ))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result'], expected)
        self.assertEqual(value['model_calls'], 1)
        summarizer.validate_outcome(value, prepared)

    def test_cli_accepts_distill_request_above_legacy_summary_wire_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'fake-codex'
            with patch.dict(os.environ, MINDIE_CODEX_BIN=str(fake)):
                prepared = self.prepared(content='x' * 140000)
                expected = self.decision(prepared)
                fake.write_text('#!' + sys.executable + '\n' + '\n'.join(
                    f'print({json.dumps(row)!r}, flush=True)' for row in [
                        {'type': 'turn.started'},
                        {'type': 'item.completed',
                         'item': {'type': 'agent_message', 'phase': 'final_answer',
                                  'text': json.dumps(expected)}},
                        {'type': 'turn.completed',
                         'usage': {'input_tokens': 1, 'cached_input_tokens': 0,
                                   'output_tokens': 1}},
                    ]), encoding='utf-8')
                fake.chmod(0o700)
                self.assertGreater(len(json.dumps(prepared)), agent_worker.MAX_REQUEST_BYTES)
                completed = subprocess.run(
                    [sys.executable, str(SCRIPTS / 'agent_worker.py')],
                    input=json.dumps(prepared), text=True, capture_output=True, timeout=15,
                    env=dict(os.environ))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result'], expected)
        summarizer.validate_outcome(value, prepared)

    def test_summary_path_starts_when_the_new_c6_api_is_unavailable(self):
        with patch.object(agent_worker, '_distill_capability', return_value=([], None)):
            payload = request(agent_worker)
            self.assertEqual(payload['policy_identity']['capabilities'], [])
            metadata = result(payload)
            def native(command, prompt, **options):
                completion(options['receipt'])
                options['receipt']['final_message'] = json.dumps(metadata)
            with patch.object(agent_worker, 'run_codex', native):
                value = agent_worker.run(payload)
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result'], metadata)
        summarizer.validate_outcome(value, payload)

    def test_ordinary_summary_request_does_not_enter_the_c6_branch(self):
        payload = request(agent_worker)
        metadata = result(payload)
        def native(command, prompt, **options):
            completion(options['receipt'])
            options['receipt']['final_message'] = json.dumps(metadata)
        with patch.object(agent_worker, 'run_codex', native), \
                patch('organizer_distill.decide') as decide:
            value = agent_worker.run(payload)
        decide.assert_not_called()
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['result'], metadata)
        summarizer.validate_outcome(value, payload)

    def test_future_distill_schema_fails_closed_before_model(self):
        prepared = self.prepared()
        future = dict(prepared, schema_version='mindie-distill-request/2')
        with patch.object(agent_worker, 'run_codex') as native, \
                self.assertRaisesRegex(summarizer.SummaryInputError,
                                       'unsupported distillation request schema'):
            agent_worker.run(future)
        native.assert_not_called()

    def test_base_request_without_worker_identity_never_reaches_model(self):
        evidence = [dict(kind='hardware_spec', outcome='verified',
                         source_locator='fixture://case',
                         content='Synthetic hardware specification.',
                         evidence_refs=['ref-1'])]
        with patch('mindie_knowledge.loop.transcript_redaction.redact_tree',
                   side_effect=lambda text, **options: (text, [])):
            base = distill.prepare_request(
                evidence, session_digest=SESSION, task_state='verified_milestone',
                redactor_executable='/bin/true', redaction_key='synthetic-key')
        with patch.object(agent_worker, 'run_codex') as native, \
                self.assertRaises(summarizer.SummaryInputError):
            agent_worker.run(base)
        native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
