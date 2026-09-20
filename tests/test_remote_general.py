"""Mechanism checks use isolated real SQLite/process state; no model calls."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import mcp_gate
import runtime_call


class GeneralRemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'python': sys.executable, 'engine_config': '/absent/not-read'}))
        self.env = patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(self.config), MINDIE_REMOTE_STATE_DIR=str(self.root / 'remote'))
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def request(self, identity=1, turn='turn-1', session='task-A'):
        return {'id': identity, 'params': {'name': 'remote_bash', 'arguments': {'command': 'true', 'host': 'example.invalid'}, '_meta': {'threadId': session, 'x-codex-turn-metadata': {'thread_id': session, 'session_id': session, 'turn_id': turn}}}}

    def test_no_activation_no_engine_and_persisted_request_identity(self):
        with patch.object(mcp_gate, 'run', return_value='{"content":[], "isError":false}') as dispatch:
            gate = mcp_gate.Gate('remote')
            self.assertFalse(gate.call(self.request())['isError'])
            self.assertTrue(mcp_gate.Gate('remote').call(self.request())['isError'])
            self.assertFalse(gate.call(self.request(turn='turn-2'))['isError'])
            self.assertFalse(gate.call(self.request(session='task-B'))['isError'])
            self.assertEqual(dispatch.call_count, 3)
        self.assertFalse(self.config.with_suffix('.sessions.sqlite3').exists())
        self.assertIsNone(gate.sessions)

    def test_pause_recovery_keeps_failed_receipts(self):
        with patch.object(mcp_gate, 'run', side_effect=TimeoutError) as dispatch:
            for i in range(10):
                self.assertTrue(mcp_gate.Gate('remote').call(self.request(i))['isError'])
            self.assertEqual(dispatch.call_count, 3)
        receipts = mcp_gate.RemoteReceipts('task-A')
        receipts.recover()
        with patch.object(mcp_gate, 'run', return_value='{"content":[], "isError":false}') as dispatch:
            self.assertTrue(mcp_gate.Gate('remote').call(self.request(0))['isError'])
            self.assertFalse(mcp_gate.Gate('remote').call(self.request(11))['isError'])
            self.assertEqual(dispatch.call_count, 1)

    def test_corrupt_receipts_fail_closed_before_dispatch(self):
        receipts = mcp_gate.RemoteReceipts('task-A')
        receipts.path.parent.mkdir(parents=True)
        receipts.path.write_bytes(b'corrupt')
        with patch.object(mcp_gate, 'run') as dispatch:
            self.assertTrue(mcp_gate.Gate('remote').call(self.request())['isError'])
            dispatch.assert_not_called()

    def test_no_lifetime_receipt_eviction_or_call_ceiling(self):
        receipts = mcp_gate.RemoteReceipts('task-A')
        for i in range(4100):
            self.assertTrue(receipts.claim(str(i)))
            receipts.finish(str(i), True)
        self.assertFalse(receipts.claim('0'))
        with sqlite3.connect(receipts.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 4100)

    def test_remote_runtime_uses_separate_job_roots(self):
        from remote_dev.core.state_store import find_job_record, state_root
        seen = []
        def dispatch(name, args):
            root = state_root()
            seen.append(root)
            if len(seen) == 1:
                marker = root / 'endpoints/target/jobs/job-test-123.json'
                marker.parent.mkdir(parents=True)
                marker.write_text(json.dumps({'job_id': 'job-test-123'}))
            else:
                self.assertIsNone(find_job_record('job-test-123'))
            return {'text': 'ok', 'result': {'outcome': 'success'}}
        with patch.dict(os.environ), patch('remote_dev.mcp.tools.call_tool', side_effect=dispatch), patch('remote_dev.core.rpc_transport.close_connections'):
            for task in ('task-A', 'task-B'):
                self.assertFalse(runtime_call.call({'surface': 'remote', 'remote_session_id': task, 'name': 'remote_job_status', 'arguments': {'job_id': 'job-test-123'}})['isError'])
        self.assertNotEqual(seen[0], seen[1])
        self.assertFalse(self.config.with_suffix('.sessions.sqlite3').exists())
