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
    def invoke(self, result):
        captured = []
        def native(command, prompt, **options):
            captured.append((command, prompt, options))
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        with patch.object(agent_worker, 'run_codex', native):
            value = agent_worker.run(dict(role='summarize', text='Synthetic public result', partial=False))
        return value, captured[0]

    def test_summary_has_no_inherited_business_model_or_tools(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, CODEX_HOME=tmp):
            Path(tmp, 'config.toml').write_text('model="gpt-6-luna"\nmodel_reasoning_effort="max"\n', encoding='utf-8')
            expected = dict(title='Synthetic example', summary='Public test observation.')
            value, (command, prompt, options) = self.invoke(expected)
            self.assertEqual(value, expected)
            self.assertIn('gpt-6-luna', command)
            self.assertIn('model_reasoning_effort="low"', command)
            self.assertNotIn('model_reasoning_effort="max"', command)
            self.assertIn('--ignore-user-config', command)
            self.assertIn('--ignore-rules', command)
            self.assertIn('features.hooks=false', command)
            self.assertIn('features.shell_tool=false', command)
            self.assertEqual(options['timeout'], 35)

    def test_user_model_settings_are_not_an_api(self):
        with patch.object(agent_worker, 'run_codex') as native:
            with self.assertRaises(TypeError):
                agent_worker.run(dict(role='summarize', text='source'), model='user-model')
        native.assert_not_called()

    def test_result_rejects_body_conditions_entries_and_invalid_metadata(self):
        for result in (dict(title='x', summary='s', content='replacement'),
                       dict(title='x', summary='s', conditions={}), dict(entries=[]),
                       dict(title=None, summary='s'), dict(title='x', summary='')):
            with self.subTest(result_keys=list(result)), self.assertRaises(agent_worker.InvalidResultError):
                self.invoke(result)

    def test_removed_organizer_cannot_be_called_under_old_role(self):
        with patch.object(agent_worker, 'run_codex') as native:
            with self.assertRaises(agent_worker.ConfigurationError):
                agent_worker.run(dict(role='organize', increment='must not rewrite'))
        native.assert_not_called()

    def test_valid_metadata_is_not_rejected_by_arbitrary_byte_limits(self):
        expected = dict(title='Public case', summary='中文' * 4096)
        result, _ = self.invoke(expected)
        self.assertEqual(result, expected)
