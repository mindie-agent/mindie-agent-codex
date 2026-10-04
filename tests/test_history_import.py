"""Explicit entry and real local history import; all sources are synthetic."""
import json
import os
import subprocess
import sys
from contextlib import closing
from unittest.mock import patch

from tests.test_sharing import SharingFixture, SCRIPTS
from tests.test_parallel_codex_contract import installed_scanner

sys.path.insert(0, str(SCRIPTS))
import codex_transcript
import consent
import history_import
import session_gate
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.store import Store


class HistoryImportTests(SharingFixture):
    def setUp(self):
        super().setUp()
        session_gate.bind_explicit_config(self.config)
        self.write_sharing()
        self.authority = Admission(self.admission)
        self.source = self.root / 'selected.jsonl'
        self.make_source()
        engine = json.loads(self.engine.read_text())
        engine.update(capture_mode='public-transcript',
                      transcript_adapter=str(SCRIPTS / 'codex_transcript.py'),
                      summary_command=[sys.executable, '-c', 'print(\'{"title":"Synthetic case","summary":"Synthetic reported result."}\')'],
                      redactor_executable=installed_scanner())
        self.engine.write_text(json.dumps(engine))

    def tearDown(self):
        session_gate.bind_explicit_config(None)
        super().tearDown()

    def make_source(self, text=None, root=None):
        rows = [dict(type='session_meta', payload=dict(id='historical-task', cwd=str(root or self.scope)))]
        if text:
            rows.append(dict(type='response_item', timestamp='2020-01-01T00:00:00Z',
                             payload=dict(type='message', role='user',
                                          content=[dict(type='input_text', text=text)])))
        self.source.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def activate(self):
        self.authority.activate('manual-A', project_root=str(self.scope))

    def run_import(self):
        receipts = []
        code = history_import.run_imports([str(self.source)], emit=receipts.append)
        return code, receipts

    def test_unactivated_task_is_denied_before_parser_or_store(self):
        with patch('mindie_knowledge.loop.cli.load_transcript_adapter', side_effect=AssertionError('no parser')):
            with self.assertRaises(ValueError):
                self.run_import()
        self.assertFalse((self.root / 'data').exists())
        self.assertIsNone(self.authority.active_lease('manual-A'))

    def test_disabled_sharing_is_denied_without_reasking_or_reading(self):
        self.activate()
        self.write_sharing(enabled=False)
        consent.record_choice('read-only')
        saved = consent.consent_path().read_bytes()
        with patch('mindie_knowledge.loop.cli.load_transcript_adapter', side_effect=AssertionError('no parser')):
            with self.assertRaises(ValueError):
                self.run_import()
        self.assertEqual(consent.consent_path().read_bytes(), saved)
        self.assertFalse((self.root / 'data').exists())

    def test_missing_summary_configuration_errors_before_source_or_store(self):
        self.activate()
        engine = json.loads(self.engine.read_text())
        del engine['summary_command']
        self.engine.write_text(json.dumps(engine))
        with patch('mindie_knowledge.loop.cli.load_transcript_adapter', side_effect=AssertionError('no parser')):
            with self.assertRaisesRegex(history_import.ConfigurationError, 'summary worker'):
                self.run_import()
        self.assertFalse((self.root / 'data').exists())
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / 'bridge.py'), '--config', str(self.config),
             'history-import', '--source', str(self.source)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('summary worker', json.loads(result.stdout)['detail'])

    def test_source_scope_is_checked_before_public_messages(self):
        self.activate()
        self.make_source('out-of-scope canary', root=self.root / 'other-project')
        with patch('mindie_knowledge.loop.cli.load_transcript_adapter', return_value=codex_transcript):
            with patch.object(codex_transcript, 'read_material', side_effect=AssertionError('no public read')):
                code, rows = self.run_import()
        self.assertEqual(code, 1)
        self.assertIn('scope', rows[0]['detail'])

    def test_import_reuses_consent_and_never_activates_historical_task(self):
        self.activate()
        consent.record_choice('contribute')
        shared = json.loads(self.community.read_text())
        shared['consent_config'] = str(consent.consent_path())
        self.community.write_text(json.dumps(shared))
        saved = consent.consent_path().read_bytes()
        engine = json.loads(self.engine.read_text())
        engine['redactor_executable'] = installed_scanner()
        engine['summary_command'] = [sys.executable, '-c', 'raise AssertionError("scheduled, not inline")']
        self.engine.write_text(json.dumps(engine))
        self.make_source('Synthetic historical experiment produced eight output tokens.')
        with patch('mindie_knowledge.loop.cli.ensure_service', return_value={'ready': True}) as service:
            first = self.run_import()
            second = self.run_import()
        self.assertEqual(first[0], 0, first)
        self.assertEqual(first[1][0]['status'], 'imported')
        self.assertEqual(first[1][0]['summary']['status'], 'pending')
        self.assertEqual(second[1][0]['status'], 'unchanged')
        self.assertEqual(second[1][0]['summary']['status'], 'pending')
        self.assertEqual(first[1][-1]['publication'], 'pending')
        self.assertEqual(service.call_count, 2)
        self.assertEqual(consent.consent_path().read_bytes(), saved)
        self.assertIsNone(self.authority.active_lease('historical-task'))
        with closing(Store(self.root / 'data', 'test')) as store:
            self.assertEqual(len(store.drafts_changed()), 1)
            tasks = [dict(row) for row in store.db.execute('SELECT * FROM transcript_tasks')]
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]['summary_status'], 'pending')
            self.assertEqual(json.loads(tasks[0]['authorization'])['session'], 'manual-A')
            self.assertEqual(store.db.execute('SELECT count(*) FROM captures').fetchone()[0], 0)

    def test_bridge_dispatches_explicit_empty_source_without_service(self):
        self.activate()
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / 'bridge.py'), '--config', str(self.config),
             'history-import', '--source', str(self.source)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['status'], 'empty')
        self.assertFalse((self.root / 'data/test/connection.json').exists())

    def test_bridge_import_saves_body_and_prepares_real_knowledge_service(self):
        self.activate()
        self.make_source('Synthetic historical bridge import: verified eight output tokens.')
        engine = json.loads(self.engine.read_text())
        engine.update(redactor_executable=installed_scanner(), feeds=[])
        self.engine.write_text(json.dumps(engine))
        # Real CLI -> core store -> service. Sharing's 300-second quiet window
        # stays unexpired and this fixture stops its exact service in teardown;
        # no public submission/model is run.
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / 'bridge.py'), '--config', str(self.config),
             'history-import', '--source', str(self.source)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]['status'], 'imported')
        self.assertEqual(rows[-1]['service'], 'ready')
        from mindie_knowledge.loop.cli import connect, rpc
        lease = self.authority.check('manual-A')
        material = rpc(connect(engine), 'explain', dict(ref=rows[0]['ref'],
                        _session_id='manual-A', _activation=lease['token']))
        self.assertIn('verified eight output tokens', material['content'])
        self.assertIsNone(self.authority.active_lease('historical-task'))

    def test_missing_sources_do_not_discover_history(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / 'bridge.py'), '--config', str(self.config), 'history-import'],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--source', result.stderr)
        self.assertFalse((self.root / 'data').exists())

    def test_native_metadata_identity_is_not_guessed(self):
        self.source.write_text(json.dumps(dict(type='session_meta', payload=dict(id='historical-task'))) + '\n')
        with self.assertRaises(ValueError):
            codex_transcript.history_source(self.source)
        self.source.write_text('[]\n')
        with self.assertRaises(ValueError):
            codex_transcript.history_source(self.source)

    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('POSIX FIFO mechanism')
        fifo = self.root / 'pipe'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            codex_transcript.history_source(fifo)
