"""Automatic current-task capture and delivery without user maintenance steps."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import admission_ops
import agent_diagnostics
import codex_transcript
import diagnostic_support
import sharing
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.store import Store


class SilentCaptureTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.profile = self.root / 'profile'
        self.config = self.root / 'adapter.json'
        self.engine = self.root / 'engine.json'
        self.community = self.root / 'community.json'
        self.admission = self.root / 'admission.sqlite3'
        self.scope = self.root / 'project'
        self.scope.mkdir()
        self.enabled = time.time() - 20
        self.settings = dict(schema='mindie-community-config/1', enabled=True,
            generation='g1', enabled_at=self.enabled, repository='mindie-agent/knowledge',
            branch='main', project_roots=[str(self.scope)], idle_seconds=300, visibility='public')
        self.community.write_text(json.dumps(self.settings))
        self.engine.write_text(json.dumps(dict(root=str(self.root / 'data'), domain='test',
            admission_path=str(self.admission), community_config=str(self.community),
            redactor_executable=str(self.root / 'scanner'), transcript_adapter=str(SCRIPTS / 'codex_transcript.py'))))
        self.config.write_text(json.dumps(dict(admission_path=str(self.admission),
            engine_config=str(self.engine), community_config=str(self.community))))
        store = Store(self.root / 'data', 'test')
        store.db.commit()
        store.close()
        self.stack.enter_context(patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(self.config),
            CODEX_HOME=str(self.profile), MINDIE_DIAGNOSTICS_ROOT=str(self.root / 'diagnostics')))
        self.stack.enter_context(patch.object(sharing, 'consent_allows', return_value=True))
        self.stack.enter_context(patch('mindie_knowledge.loop.handoff._probe', return_value='absent'))
        self.wake = self.stack.enter_context(patch('mindie_knowledge.loop.handoff.request_wake',
            return_value=dict(wake='requested', runtime='unavailable', reason='requested')))

    def event(self, *, owner='current', session='current', scope=None, fork=False):
        path = self.profile / 'sessions/current.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        created = self.enabled - 5 if not fork else self.enabled + 3
        meta = dict(id=owner, cwd=str(scope or self.scope), timestamp=stamp(created))
        if fork:
            meta['forked_from_id'] = 'parent'
        rows = [dict(type='session_meta', timestamp=stamp(created), payload=meta)]
        for ts, text in ((self.enabled-1, 'OLD_OFF_WINDOW'), (self.enabled+1, 'FIRST_TURN'),
                         (self.enabled+4, 'AFTER_FORK')):
            rows.append(dict(type='response_item', timestamp=stamp(ts), payload=dict(
                type='message', role='user', content=[dict(type='input_text', text=text)])))
        path.write_bytes(('\n'.join(json.dumps(row) for row in rows)+'\n').encode())
        return dict(hook_event_name='Stop', identity_kind='turn', session_id=session,
                    turn_id='turn1', transcript_path=str(path), cwd=str(self.scope))

    def accept(self, event):
        return admission_ops.operation('stop_capture', dict(event=event))

    def test_first_stop_associates_and_keeps_first_authorized_turn(self):
        event = self.event()
        self.assertFalse(self.admission.exists())
        result = self.accept(event)
        self.assertEqual(result['stage'], 'accepted-local')
        lease = Admission(self.admission).check('current')
        self.assertEqual(lease['activated_at'], self.enabled)
        with closing(sqlite3.connect(self.root / 'data/test/state-v4.sqlite3')) as db:
            boundary = db.execute('SELECT boundary FROM captures').fetchone()[0]
        captured = codex_transcript.read_material(event['transcript_path'], 0,
            session_id='current', not_before=boundary)
        self.assertIn('FIRST_TURN', captured['text'])
        self.assertNotIn('OLD_OFF_WINDOW', captured['text'])
        self.assertEqual(self.accept(event)['capture_id'], result['capture_id'])

    def test_scope_and_sibling_artifact_cannot_create_a_binding(self):
        result = self.accept(self.event(scope=self.root / 'other'))
        self.assertEqual(result['reason'], 'out-of-scope')
        self.assertFalse(self.admission.exists())
        with self.assertRaisesRegex(ValueError, 'another task'):
            self.accept(self.event(owner='sibling'))
        self.assertFalse(self.admission.exists())
        self.wake.assert_not_called()

    def test_fork_boundary_and_explicit_task_revocation_are_preserved(self):
        event = self.event(fork=True)
        self.accept(event)
        store = Admission(self.admission)
        lease = store.check('current')
        captured = codex_transcript.read_material(event['transcript_path'], 0,
            session_id='current', not_before=lease['activated_at'])
        self.assertNotIn('FIRST_TURN', captured['text'])
        self.assertIn('AFTER_FORK', captured['text'])
        store.deactivate('current')
        self.assertEqual(self.accept(event)['reason'], 'task-revoked')
        self.assertEqual(store.inspect('current')['status'], 'inactive')

    def test_disabled_contribution_reads_no_transcript_or_state(self):
        self.settings.update(enabled=False, enabled_at=None)
        self.community.write_text(json.dumps(self.settings))
        with patch.object(codex_transcript, 'capture_source', side_effect=AssertionError('read')):
            self.assertEqual(self.accept(dict(session_id='current'))['reason'], 'sharing-disabled')
        self.assertFalse(self.admission.exists())

    def test_real_stdio_delivers_pending_fault_once_on_natural_call(self):
        agent_diagnostics.enqueue('mindie-knowledge', 'capture', 'summary', 'worker_failed',
                                  dict(incident_id='a'*32, logging_failed=False))
        frames = [dict(jsonrpc='2.0', id=1, method='initialize', params={}),
                  dict(jsonrpc='2.0', id=2, method='tools/call', params=dict(
                      name='knowledge_query', arguments=dict(query='synthetic'),
                      _meta={'threadId':'current', 'x-codex-turn-metadata': {
                          'thread_id':'current', 'session_id':'current', 'turn_id':'turn'}}))]
        result = subprocess.run([sys.executable, str(SCRIPTS / 'bridge.py'), 'mcp'],
            input=''.join(json.dumps(frame)+'\n' for frame in frames), text=True,
            capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = next(json.loads(line)['result'] for line in result.stdout.splitlines()
                        if json.loads(line).get('id') == 2)
        projection = response['structuredContent']['agent_diagnostics']
        self.assertEqual(projection['items'][0]['code'], 'worker_failed')
        self.assertIsNone(agent_diagnostics.pending())
        self.assertNotIn('run status', result.stdout.lower())
        self.assertFalse(self.admission.exists())

    def test_internal_diagnostics_persist_until_a_written_response_is_acknowledged(self):
        agent_diagnostics.enqueue('mindie-agent-codex', 'capture.stop', 'handoff', 'failed',
                                  dict(incident_id='a'*32, logging_failed=False))
        value = dict(content=[dict(type='text', text='business result')], isError=False)
        projected = diagnostic_support.attach_pending(value)
        self.assertFalse(projected['isError'])
        self.assertEqual(projected['content'], value['content'])
        self.assertIsNotNone(agent_diagnostics.pending())  # Not acknowledged by serialization.
        agent_diagnostics.enqueue('mindie-agent-codex', 'capture.stop', 'handoff', 'failed',
                                  dict(incident_id='b'*32, logging_failed=False))
        diagnostic_support.acknowledge_pending(projected)
        self.assertEqual(agent_diagnostics.pending()['items'][0]['count'], 1)
        diagnostic_support.acknowledge_pending(diagnostic_support.attach_pending(value))
        self.assertIsNone(agent_diagnostics.pending())

    def test_projection_overflow_is_counted_and_bootstrap_copy_matches_core(self):
        for index in range(70):
            agent_diagnostics.enqueue('mindie-agent-codex', 'capture.stop', 'handoff', 'code'+str(index), {})
        projected = agent_diagnostics.pending(limit=100)
        self.assertEqual(sum(item['count'] for item in projected['items']), 70)
        self.assertEqual(len(projected['items']), 65)
        self.assertEqual(next(item for item in projected['items'] if item['code']=='projection_overflow')['count'], 6)
        from mindie_knowledge.loop import agent_diagnostics as core
        self.assertEqual(Path(core.__file__).read_bytes(), Path(agent_diagnostics.__file__).read_bytes())

    def test_failed_diagnostic_recorder_remains_pending_without_masking_results(self):
        with patch.object(diagnostic_support, '_record', side_effect=OSError('synthetic recorder fault')):
            result = diagnostic_support.failure('capture.stop', 'handoff', 'failed')
        self.assertTrue(result['logging_failed'])
        self.assertEqual(agent_diagnostics.pending()['items'][0]['code'], 'failed')
        malformed = dict(content=[], isError=True, structuredContent='not-an-object')
        self.assertIs(diagnostic_support.attach_pending(malformed), malformed)
        self.assertIsNotNone(agent_diagnostics.pending())

    def test_attach_never_adds_a_user_status_command(self):
        source = dict(content=[dict(type='text', text='failed business operation')], isError=True)
        result = diagnostic_support.attach(source, dict(incident_id='a'*32, logging_failed=False))
        self.assertEqual(result['content'], source['content'])
        self.assertEqual(result['diagnostic']['incident_id'], 'a'*32)

    def test_stable_stop_failure_before_bridge_stays_silent_and_pending(self):
        stable = self.root / 'installation'
        stable.mkdir()
        for name in ('runtime_launcher.py', 'update_lock.py', 'diagnostic_support.py',
                     'diagnostic_fallback.py', 'agent_diagnostics.py'):
            shutil.copy2(SCRIPTS / name, stable / name)
        for broken, expected in ((self.root / 'missing.json', 'generation_unavailable'),
                                 (self.config, 'generation_busy')):
            from update_lock import update_lock
            with ExitStack() as held:
                if broken == self.config:
                    held.enter_context(update_lock(self.config, exclusive=True))
                result = subprocess.run([sys.executable, str(stable / 'runtime_launcher.py'),
                    '--config', str(broken), 'stop'], capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout.strip(), result.stderr), (1, '{}', ''))
            projection = agent_diagnostics.pending()
            self.assertIn(expected, [item['code'] for item in projection['items']])
            agent_diagnostics.acknowledge(projection)
