"""K3-derived native worker boundaries; complete batches replace sampled input."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import agent_worker
from mindie_knowledge.materials import summarizer
from k3_material_fixture import request, result, completion


class SummaryModelTests(unittest.TestCase):
    def invoke(self, payload, metadata):
        captured = []
        def native(command, prompt, **options):
            captured.append((command, prompt, options))
            completion(options['receipt'])
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps(metadata), encoding='utf-8')
        with patch.object(agent_worker, 'run_codex', native):
            value = agent_worker.run(payload)
        return value, captured

    def test_k3_01_initial_adaptation_uses_fixed_small_model_without_tools(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, CODEX_HOME=tmp):
            Path(tmp, 'config.toml').write_text('model="business-model"\nmodel_reasoning_effort="max"\n', encoding='utf-8')
            payload = request(agent_worker)
            value, calls = self.invoke(payload, result(payload))
        command, prompt, options = calls[0]
        self.assertEqual(value['status'], 'returned')
        self.assertEqual(value['model_calls'], 1)
        self.assertEqual(value['usage'], dict(input_tokens=120, cached_input_tokens=20, output_tokens=30))
        self.assertEqual(value['billing_status'], 'reported')
        self.assertIn('gpt-5.6-luna', command)
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertNotIn('business-model', command)
        self.assertIn('--ignore-user-config', command)
        self.assertIn('--ignore-rules', command)
        self.assertIn('features.hooks=false', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertEqual(options['timeout'], summarizer.SUMMARY_TIMEOUT)
        self.assertIn('fallible reference index', prompt)
        self.assertIn('Retain failed attempts', prompt)
        summarizer.validate_outcome(value, payload)

    def test_k3_02_correction_and_navigation_share_one_native_generation(self):
        previous = dict(title='Earlier benchmark', summary='The initial checkpoint interpretation remains a hypothesis.')
        payload = request(agent_worker, 'K3-02', previous)
        value, calls = self.invoke(payload, result(payload, 'K3-02'))
        self.assertEqual(len(calls), 1)
        self.assertIn(previous['summary'], calls[0][1])
        self.assertIn('middle checkpoint correction', calls[0][1])
        self.assertEqual(value['result']['navigation']['title'], 'K3-02')
        self.assertEqual([item['block_id'] for item in value['result']['blocks']],
                         [item['block_id'] for item in payload['blocks']])

    def test_k3_03_incomplete_index_keeps_paid_usage_and_raw_output(self):
        payload = request(agent_worker, 'K3-03')
        invalid = result(payload, 'K3-03')
        invalid['blocks'] = []
        value, calls = self.invoke(payload, invalid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'invalid_result')
        self.assertEqual(value['error_reason'], 'block_count_mismatch')
        self.assertEqual(value['model_calls'], 1)
        self.assertTrue(value['usage_known'])
        self.assertEqual(json.loads(value['raw_result']), invalid)
        self.assertIsNone(value['result'])

    def test_k3_04_extra_duplicate_reports_structure_failure_with_known_cost(self):
        # Actual acceptance shape reported by the parent: three admitted blocks,
        # four indexes including a duplicate. Text and identifiers are synthetic.
        original = request(agent_worker, 'K3-04')
        blocks = [dict(block_id=summarizer.digest(['K3-04', i]), text='Anonymous precision observation.',
                       source_range=dict(case='K3-04', part=i)) for i in range(3)]
        payload = summarizer.make_request(task_id=original['task_id'], body_version=original['body_version'],
            blocks=blocks, prior_navigation=None, identity=agent_worker.identity())
        invalid = result(payload, 'K3-04')
        invalid['blocks'].append(dict(invalid['blocks'][-1]))
        value, calls = self.invoke(payload, invalid)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error_reason'], 'block_count_mismatch')
        self.assertEqual(value['billing_status'], 'reported')
        self.assertEqual(value['usage']['input_tokens'], 120)
        self.assertEqual(value['usage']['output_tokens'], 30)
        self.assertEqual(len(calls), 1)
        self.assertEqual(summarizer.failure_detail(value)['stage'], 'index-validation')
        # Same count with a duplicate is a different structural failure.
        invalid['blocks'].pop()
        invalid['blocks'][1]['block_id'] = invalid['blocks'][0]['block_id']
        value, calls = self.invoke(payload, invalid)
        self.assertEqual(value['error_reason'], 'block_identity_mismatch')
        self.assertTrue(value['usage_known'])
        self.assertEqual(len(calls), 1)

    def test_k3_04_interruption_is_unknown_and_never_reinvoked(self):
        payload = request(agent_worker, 'K3-04')
        def interrupted(command, prompt, **options):
            options['receipt'].update(native_started=True, turn_started=True)
            raise TimeoutError('private provider details must not escape')
        with patch.object(agent_worker, 'run_codex', interrupted) as native:
            value = agent_worker.run(payload)
        self.assertEqual(value['status'], 'outcome_unknown')
        self.assertEqual(value['error'], 'deadline')
        self.assertEqual(value['billing_status'], 'unknown')
        self.assertEqual(value['model_calls'], 1)
        self.assertIsNone(value['usage'])
        self.assertNotIn('private provider', json.dumps(value))

    def test_k3_04_changed_implementation_cannot_reuse_old_policy(self):
        payload = request(agent_worker, 'K3-04')
        with patch.object(agent_worker, 'SUMMARY_MODEL', 'another-fixed-model'), patch.object(agent_worker, 'run_codex') as native:
            value = agent_worker.run(payload)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'configuration')
        self.assertEqual(value['model_calls'], 0)
        self.assertEqual(value['billing_status'], 'not_called')
        native.assert_not_called()

    def test_k3_01_missing_binary_is_known_uncalled_failure(self):
        with patch.dict(os.environ, MINDIE_CODEX_BIN='/missing/k3-small-model'):
            payload = request(agent_worker)
            value = agent_worker.run(payload)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'configuration')
        self.assertEqual(value['model_calls'], 0)
        self.assertEqual(value['billing_status'], 'not_called')

    def test_k3_03_body_replacement_is_not_metadata(self):
        payload = request(agent_worker, 'K3-03')
        metadata = result(payload, 'K3-03')
        metadata['content'] = 'Rewritten body would lose the original pending observation.'
        value, _ = self.invoke(payload, metadata)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['error'], 'invalid_result')
        self.assertIsNone(value['result'])


if __name__ == '__main__':
    unittest.main()
