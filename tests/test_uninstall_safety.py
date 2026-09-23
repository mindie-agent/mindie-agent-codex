"""Focused destructive-boundary regression, separate from real launchd evidence."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'))
import auto_update


class UninstallSafetyTests(unittest.TestCase):
    def test_unknown_schedule_state_retains_executables_and_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'owned'; root.mkdir()
            config = base / 'adapter.json'; config.write_text('{}')
            settings = base / 'updater.json'
            settings.write_text(json.dumps({'root': str(root), 'adapter_config': str(config)}))
            for name in ('generations/unused/plugin/script.py', 'controller/updater.py',
                         'launcher.py', 'state.json', 'transaction.json'):
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}' if name.endswith('.json') else '# retained\n')
            before = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()}
            with patch.object(auto_update, 'schedule_disable', side_effect=RuntimeError('state unproven')) as removal:
                result = auto_update.uninstall(types.SimpleNamespace(settings=settings, purge=True))
            self.assertEqual(result['status'], 'refused')
            self.assertEqual(removal.call_count, 1)
            self.assertEqual(result['removed_generations'], [])
            self.assertTrue(all((base / key).read_bytes() == value for key, value in before.items()))


if __name__ == '__main__':
    unittest.main()
