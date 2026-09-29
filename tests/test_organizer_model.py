"""Metadata boundary: independent model selection, no body-writing capability."""
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


class SummaryModelTests(unittest.TestCase):
    def invoke(self, result, **kwargs):
        captured = []
        def native(command, prompt, **options):
            captured.append((command, prompt, options))
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        with patch.object(agent_worker, 'run_codex', native):
            value = agent_worker.run(dict(role='summarize', text='Synthetic public result', partial=False), **kwargs)
        return value, captured[0]

    def test_summary_has_no_inherited_business_model_or_tools(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, CODEX_HOME=tmp):
            Path(tmp, 'config.toml').write_text('model="gpt-6-luna"\nmodel_reasoning_effort="max"\n', encoding='utf-8')
            expected = dict(title='Synthetic example', summary='Public test observation.')
            value, (command, prompt, options) = self.invoke(expected, model='gpt-6-luna')
            self.assertEqual(value, expected)
            self.assertIn('gpt-6-luna', command)
            self.assertIn('model_reasoning_effort="low"', command)
            self.assertNotIn('model_reasoning_effort="max"', command)
            self.assertIn('--ignore-user-config', command)
            self.assertIn('--ignore-rules', command)
            self.assertIn('features.hooks=false', command)
            self.assertIn('features.shell_tool=false', command)
            self.assertEqual(options['timeout'], 35)

    def test_explicit_effort_is_forwarded_without_rewriting_the_body(self):
        expected = dict(title='Example', summary='Observed public result.')
        for effort in ('none', 'low', 'medium', 'max'):
            value, (command, _, _) = self.invoke(expected, model='selected-model', reasoning_effort=effort)
            self.assertEqual(value, expected)
            self.assertIn(f'model_reasoning_effort="{effort}"', command)

    def test_missing_or_invalid_configuration_never_silently_falls_back(self):
        with patch.object(agent_worker, 'run_codex') as native:
            for kwargs in ({}, dict(model=''), dict(model='summary-model', reasoning_effort='invented')):
                with self.assertRaises(agent_worker.ConfigurationError):
                    agent_worker.run(dict(role='summarize', text='source'), **kwargs)
        native.assert_not_called()

    def test_result_rejects_body_conditions_entries_and_invalid_metadata(self):
        for result in (dict(title='x', summary='s', content='replacement'),
                       dict(title='x', summary='s', conditions={}), dict(entries=[]),
                       dict(title=None, summary='s'), dict(title='x', summary='中' * 1000)):
            with self.subTest(result_keys=list(result)), self.assertRaises(agent_worker.InvalidResultError):
                self.invoke(result, model='synthetic-summary-model')

    def test_removed_organizer_cannot_be_called_under_old_role(self):
        with patch.object(agent_worker, 'run_codex') as native:
            with self.assertRaises(agent_worker.ConfigurationError):
                agent_worker.run(dict(role='organize', increment='must not rewrite'), model='synthetic')
        native.assert_not_called()

    def test_oversized_wire_output_keeps_its_distinct_failure(self):
        with self.assertRaises(agent_worker.OutputLimitExceeded):
            self.invoke(dict(title='x', summary='x' * 8192), model='synthetic-summary-model')
