"""Focused destructive-boundary regression, separate from real launchd evidence."""
from contextlib import ExitStack
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

    def managed(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        base = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        root = base / 'updates'
        root.mkdir()
        config = base / 'adapter.json'
        settings = base / 'updater.json'
        for name in ('current', 'candidate', 'leased', 'retired', 'untracked'):
            generation = root / 'generations' / name
            (generation / 'plugin/scripts').mkdir(parents=True)
            if name != 'untracked':
                auto_update.atomic(generation / 'ownership.json',
                    dict(schema='mindie-runtime-generation/2', revision=name))
            (generation / 'plugin/scripts/entry.py').write_text('# owned fixture\n')
        auto_update.atomic(config, dict(runtime_scripts=str(root / 'generations/current/plugin/scripts')))
        auto_update.atomic(settings, dict(root=str(root), adapter_config=str(config)))
        auto_update.atomic(root / 'state.json', dict(current=dict(revision='current',
            plugin=str(root / 'generations/current/plugin')), candidate='candidate'))
        (root / 'launcher.py').write_text('# updater fixture\n')
        (root / 'runtime_launcher.py').write_text('# retained native entry\n')
        (root / 'generation-locks').mkdir()
        return base, root, config, settings

    def test_uninstall_keeps_active_generation_and_current_candidate_and_untracked(self):
        _base, root, config, settings = self.managed()
        original = auto_update.shutil.rmtree
        checked = []
        def remove(path, *args, **kwargs):
            if Path(path).parent == root / 'generations':
                with self.assertRaises(BlockingIOError):
                    with auto_update.file_lock(config.with_suffix('.update.lock'), exclusive=True):
                        pass
                checked.append(Path(path).name)
            return original(path, *args, **kwargs)
        with auto_update.file_lock(root / 'generation-locks/leased.lock'), \
             patch.object(auto_update, 'schedule_disable'), \
             patch.object(auto_update.shutil, 'rmtree', side_effect=remove):
            result = auto_update.uninstall(types.SimpleNamespace(settings=settings, purge=False))
        self.assertEqual(result['status'], 'uninstalled')
        self.assertEqual(checked, ['retired'])
        self.assertEqual({p.name for p in (root / 'generations').iterdir()},
                         {'current', 'candidate', 'leased', 'untracked'})
        self.assertTrue((root / 'runtime_launcher.py').exists())
        self.assertFalse((root / 'launcher.py').exists())
        self.assertTrue((root / 'checker.lock').exists())

    def test_interrupted_transaction_preserves_all_generations_and_recovery_launcher(self):
        _base, root, _config, settings = self.managed()
        auto_update.atomic(root / 'transaction.json', {})
        before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        with patch.object(auto_update, 'schedule_disable') as removal:
            result = auto_update.uninstall(types.SimpleNamespace(settings=settings, purge=True))
        self.assertEqual(removal.call_count, 1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['schedule'], 'schedule removed')
        self.assertEqual(result['retention']['reason'], 'transaction_pending')
        self.assertEqual(result['removed_generations'], [])
        self.assertTrue(all((root / path).read_bytes() == content for path, content in before.items()))

    def test_purge_keeps_reachable_or_untracked_generations(self):
        _base, root, _config, settings = self.managed()
        with patch.object(auto_update, 'schedule_disable'):
            result = auto_update.uninstall(types.SimpleNamespace(settings=settings, purge=True))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['schedule'], 'schedule removed')
        self.assertTrue((root / 'generations/current').exists())
        self.assertTrue((root / 'generations/candidate').exists())
        self.assertTrue((root / 'generations/untracked').exists())
        self.assertTrue(settings.exists())
        self.assertEqual(result['recovery_metadata'], 'preserved')

    def test_cleanup_failure_preserves_completed_schedule_cancellation(self):
        _base, root, _config, settings = self.managed()
        def remove(_path, *args, **kwargs):
            raise PermissionError('synthetic cleanup refusal')
        with patch.object(auto_update, 'schedule_disable'), \
             patch.object(auto_update.shutil, 'rmtree', side_effect=remove):
            result = auto_update.uninstall(types.SimpleNamespace(settings=settings, purge=False))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['schedule'], 'schedule removed')
        self.assertEqual(result['retention']['status'], 'failed')
        self.assertEqual(result['retention']['error_type'], 'PermissionError')
        self.assertTrue((root / 'launcher.py').exists())


if __name__ == '__main__':
    unittest.main()
