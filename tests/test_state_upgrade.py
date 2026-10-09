"""Version boundary protects remote effects as well as knowledge material."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import mcp_gate
import receipt_layout
import state_compatibility


class ReceiptUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.location = patch.object(mcp_gate, 'remote_state_dir', return_value=self.root)
        self.location.start()
        self.owners = []

    def tearDown(self):
        for owner in self.owners:
            owner._ownership.close()
        self.location.stop()
        self.temp.cleanup()

    def receipts(self):
        obj = mcp_gate.RemoteReceipts('task')
        self.owners.append(obj)
        return obj

    def test_development_resets_can_repeat_without_opening_old_receipts(self):
        legacy = self.root / 'gate/task.sqlite3'
        legacy.parent.mkdir()
        legacy.write_bytes(b'opaque old development ledger without authority marker')
        retained = []
        for version in (1, 2, 3):
            with patch.object(receipt_layout, 'FORMAT', version):
                receipts = self.receipts()
                self.assertTrue(receipts.claim('request'))
                receipts.finish('request', 'succeeded')
                retained.append(receipts.path)
        self.assertEqual(legacy.read_bytes(), b'opaque old development ledger without authority marker')
        for path in retained:
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT status FROM attempts').fetchall(), [('succeeded',)])

    def test_compatible_release_upgrade_preserves_success_and_unknown_without_replay(self):
        with patch.object(receipt_layout, 'RELEASE_VERSION', '1.0.0'):
            receipts = self.receipts()
            for identity, outcome in (('known-success', 'succeeded'), ('uncertain-write', 'unknown')):
                self.assertTrue(receipts.claim(identity))
                receipts.finish(identity, outcome)
        for release in ('1.1.0', None):
            with patch.object(receipt_layout, 'RELEASE_VERSION', release):
                upgraded = self.receipts()
                self.assertEqual(upgraded.path, receipts.path)
                self.assertFalse(upgraded.claim('known-success'))
                self.assertFalse(upgraded.claim('uncertain-write'))
        layout = self.root / 'gate-layout.json'
        self.assertEqual(json.loads(layout.read_text())['release_version'], '1.1.0')
        before = receipts.path.read_bytes()
        with patch.object(receipt_layout, 'FORMAT', 2):
            with self.assertRaisesRegex(ValueError, 'released.*migration'):
                self.receipts().claim('new')
        self.assertEqual(receipts.path.read_bytes(), before)
        self.assertFalse((self.root / 'gate-v2').exists())

    def test_missing_release_layout_cannot_reset_admission(self):
        with patch.object(receipt_layout, 'RELEASE_VERSION', '1.0.0'):
            receipts = self.receipts()
            self.assertTrue(receipts.claim('uncertain'))
            receipts.finish('uncertain', 'unknown')
        (self.root / 'gate-layout.json').unlink()
        with patch.object(receipt_layout, 'FORMAT', 2):
            with self.assertRaisesRegex(ValueError, 'layout is missing'):
                self.receipts().claim('new')
        self.assertTrue(receipts.path.is_file())

    def test_layout_publication_failure_is_not_first_use_on_retry(self):
        with patch.object(receipt_layout.os, 'replace', side_effect=OSError('synthetic layout publication failure')):
            with self.assertRaisesRegex(OSError, 'publication failure'):
                self.receipts().claim('not-executed')
        with self.assertRaisesRegex(ValueError, 'layout is missing'):
            self.receipts().claim('not-executed')

    def test_real_compatibility_helper_reads_both_layouts_without_mutating_them(self):
        from mindie_knowledge import state_layout
        from mindie_knowledge.loop.store import Store
        config = self.root / 'engine.json'
        config.write_text(json.dumps(dict(root=str(self.root / 'knowledge'), domain='demo')))
        with patch.object(state_layout, 'RELEASE_VERSION', '1.0.0'), \
                patch.object(receipt_layout, 'RELEASE_VERSION', '1.0.0'):
            Store(self.root / 'knowledge', 'demo').close()
            self.assertTrue(self.receipts().claim('consumed'))
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(state_compatibility.check(config), {'status': 'compatible'})
        with patch.object(state_layout, 'FORMAT', 2):
            with self.assertRaisesRegex(ValueError, 'released knowledge'):
                state_compatibility.check(config)
        with patch.object(receipt_layout, 'FORMAT', 2):
            with self.assertRaisesRegex(ValueError, 'released remote'):
                state_compatibility.check(config)
        self.assertEqual({str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)


if __name__ == '__main__':
    unittest.main()
