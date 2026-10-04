"""Remote admission cost follows live work; elapsed age is not owner death."""
from contextlib import closing
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
from bounded_process import ProcessResult
import mcp_gate


class ReceiptLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.objects = []
        self.location = patch.object(mcp_gate, 'remote_state_dir', return_value=self.root)
        self.location.start()

    def tearDown(self):
        for obj in self.objects:
            obj._ownership.close()
        self.location.stop()
        self.temp.cleanup()

    def receipts(self, session='task'):
        obj = mcp_gate.RemoteReceipts(session)
        self.objects.append(obj)
        return obj

    def test_claim_uses_only_running_index_despite_completed_history_growth(self):
        costs = []
        for count in (100, 10000):
            obj = self.receipts('task-' + str(count))
            with closing(obj._db()) as db, db:
                db.executemany("INSERT INTO attempts(identity,started,status,owner) VALUES(?,0,'succeeded','')",
                               ((str(i),) for i in range(count)))
                for query in ("SELECT DISTINCT owner FROM attempts WHERE status='running'",
                              "SELECT count(*) FROM attempts WHERE status='running'"):
                    plan = db.execute('EXPLAIN QUERY PLAN ' + query).fetchall()
                    self.assertIn('attempts_running_owner', str(plan))
            original = obj._db
            steps = [0]
            def tracked():
                db = original()
                def tick():
                    steps[0] += 1
                    return 0
                db.set_progress_handler(tick, 1)
                return db
            obj._db = tracked
            self.assertTrue(obj.claim('new'))
            costs.append(steps[0])
            self.assertFalse(obj.claim('0'), 'old completed identity became reusable')
            with closing(sqlite3.connect(obj.path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], count + 1)
        self.assertLess(max(costs), 500, costs)
        self.assertLessEqual(abs(costs[1] - costs[0]), 100, costs)

    def test_old_live_attempt_survives_and_dead_owner_becomes_nonreplayable_unknown(self):
        first = self.receipts()
        self.assertTrue(first.claim('original'))
        with closing(sqlite3.connect(first.path)) as db, db:
            db.execute("UPDATE attempts SET started=1 WHERE identity='original'")
        second = self.receipts()
        self.assertTrue(second.claim('other'))
        with closing(sqlite3.connect(first.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM attempts WHERE identity='original'").fetchone()[0], 'running')
        first._ownership.close()  # The OS lease is released; no clock is advanced.
        self.assertTrue(second.claim('after-owner-exit'))
        with closing(sqlite3.connect(first.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM attempts WHERE identity='original'").fetchone()[0], 'unknown')
        self.assertFalse(self.receipts().claim('original'))

    def test_existing_receipt_authority_damage_never_becomes_fresh_admission(self):
        for damage in ("missing", "empty", "table", "identity", "marker"):
            with self.subTest(damage=damage):
                obj = self.receipts("damage-" + damage)
                self.assertTrue(obj.claim("consumed"))
                if damage == "missing":
                    obj.path.unlink()
                elif damage == "empty":
                    obj.path.write_bytes(b"")
                elif damage == "marker":
                    obj.path.with_suffix(".authority.json").unlink()
                else:
                    with closing(sqlite3.connect(obj.path)) as db, db:
                        db.execute("DROP TABLE attempts" if damage == "table" else "UPDATE authority SET identity='wrong'")
                with self.assertRaises((sqlite3.Error, RuntimeError, ValueError)):
                    self.receipts("damage-" + damage).claim("consumed")

    def test_known_business_success_stays_succeeded_when_helper_cleanup_fails(self):
        config = self.root / "cleanup-config.json"
        config.write_text(json.dumps({"python": sys.executable, "engine_config": "/absent"}))
        request = {"id": 7, "params": {"name": "remote_bash",
            "arguments": {"command": "true", "host": "example.invalid"},
            "_meta": {"threadId": "cleanup", "x-codex-turn-metadata": {
                "thread_id": "cleanup", "session_id": "cleanup", "turn_id": "turn"}}}}
        completed = ProcessResult("completed", '{"content":[],"isError":false}', 0,
                                  cleanup=[{"stage": "wait", "error_type": "OSError"}])
        with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(config)), patch.object(mcp_gate, "run", return_value=completed):
            gate = mcp_gate.Gate("remote")
            result = gate.call(request)
        self.objects.extend(gate._remote_receipts.values())
        self.assertTrue(result["isError"])
        self.assertEqual(result["operation_outcome"], "succeeded")
        with closing(sqlite3.connect(gate._remote_receipts["cleanup"].path)) as db:
            self.assertEqual(db.execute("SELECT status FROM attempts").fetchone()[0], "succeeded")

    def test_same_column_names_without_constraints_or_correct_index_are_rejected(self):
        for damage in ('attempt-primary-key', 'authority-primary-key', 'index-definition'):
            with self.subTest(damage=damage):
                obj = self.receipts(damage)
                self.assertTrue(obj.claim('consumed'))
                with closing(sqlite3.connect(obj.path)) as db, db:
                    if damage == 'index-definition':
                        db.execute('DROP INDEX attempts_running_owner')
                        db.execute('CREATE INDEX attempts_running_owner ON attempts(started)')
                    else:
                        table = 'attempts' if damage == 'attempt-primary-key' else 'authority'
                        rows = db.execute('SELECT * FROM ' + table).fetchall()
                        db.execute('DROP TABLE ' + table)
                        definition = mcp_gate.RECEIPT_SCHEMA[table][2].replace(' PRIMARY KEY', '')
                        db.execute(definition)
                        db.executemany('INSERT INTO ' + table + ' VALUES(' + ','.join('?' for _ in rows[0]) + ')', rows)
                        if table == 'attempts':
                            db.execute(mcp_gate.RECEIPT_SCHEMA['attempts_running_owner'][2])
                with self.assertRaisesRegex(ValueError, 'constraints'):
                    obj.claim('new')
                with closing(sqlite3.connect(obj.path)) as db:
                    self.assertEqual(db.execute('SELECT identity FROM attempts').fetchall(), [('consumed',)])

    @unittest.skipUnless(os.name == 'posix', 'POSIX special files')
    def test_nonregular_authority_marker_is_rejected_without_reading(self):
        for kind in ('fifo', 'symlink'):
            with self.subTest(kind=kind):
                obj = self.receipts('marker-' + kind)
                self.assertTrue(obj.claim('consumed'))
                marker = obj.path.with_suffix('.authority.json')
                saved = marker.read_bytes()
                marker.unlink()
                if kind == 'fifo':
                    os.mkfifo(marker)
                else:
                    target = marker.with_suffix('.target'); target.write_bytes(saved)
                    marker.symlink_to(target)
                with self.assertRaisesRegex(ValueError, 'regular file'):
                    obj.claim('new')

    def test_gate_constructs_one_receipt_owner_per_seen_task(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'python': sys.executable, 'engine_config': '/absent/not-used'}))
        factory = mcp_gate.RemoteReceipts
        def request(identity):
            return {'id': identity, 'params': {'name': 'remote_bash',
                    'arguments': {'command': 'true', 'host': 'example.invalid'},
                    '_meta': {'threadId': 'task', 'x-codex-turn-metadata': {
                        'thread_id': 'task', 'session_id': 'task', 'turn_id': 'turn'}}}}
        with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(config)), \
             patch.object(mcp_gate, 'RemoteReceipts', wraps=factory) as constructor, \
             patch.object(mcp_gate, 'run', return_value=ProcessResult("completed", '{"content":[],"isError":false}', 0)):
            gate = mcp_gate.Gate('remote')
            self.assertFalse(gate.call(request(1))['isError'])
            self.assertFalse(gate.call(request(2))['isError'])
            self.assertEqual(constructor.call_count, 1)
            self.objects.extend(gate._remote_receipts.values())


if __name__ == '__main__':
    unittest.main()
