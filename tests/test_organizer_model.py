"""Preserve the selected native model while keeping maintenance isolated."""
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


class OrganizerModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, CODEX_HOME=str(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, **kwargs):
        captured = []
        def native(command, prompt):
            captured.append(command)
            Path(command[command.index('--output-last-message') + 1]).write_text(
                json.dumps(dict(entries=[])), encoding='utf-8')
        with patch.object(agent_worker, 'run_codex', native):
            self.assertEqual(agent_worker.run(dict(role='organize'), **kwargs), dict(entries=[]))
        return captured[0]

    def test_saved_model_survives_config_isolation(self):
        (self.root/'config.toml').write_text('model="gpt-6-luna"\nmodel_reasoning_effort="max"\n[features]\nhooks=true\nshell_tool=true\n', encoding='utf-8')
        command = self.invoke()
        self.assertEqual(command[command.index('--model')+1], 'gpt-6-luna')
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertIn('--ignore-user-config', command)
        self.assertIn('features.hooks=false', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertIn('--ignore-rules', command)

    def test_explicit_worker_selection_overrides_saved_model(self):
        (self.root/'config.toml').write_text('model="gpt-6-astra"\nmodel_reasoning_effort="high"\n', encoding='utf-8')
        command = self.invoke(model='gpt-6-luna', reasoning_effort='max')
        self.assertNotIn('gpt-6-astra', command)
        self.assertEqual(command[command.index('--model')+1], 'gpt-6-luna')

    def test_damaged_profile_does_not_silently_choose_another_model(self):
        (self.root/'config.toml').write_text('model = [', encoding='utf-8')
        with patch.object(agent_worker, 'run_codex') as native:
            with self.assertRaises(agent_worker.ConfigurationError):
                agent_worker.run(dict(role='organize'))
        native.assert_not_called()
