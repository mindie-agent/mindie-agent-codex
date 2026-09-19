"""Real SQLite admission around the domain coordinator adapter (no remote jobs)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'plugins/mindie-agent/domain-lib'),str(ROOT/'plugins/mindie-agent/scripts')]
from mindie_jobs import task_client, TaskTargetError
from session_gate import Sessions, Inactive

class ManagedAdmissionTests(unittest.TestCase):
    def test_inactive_client_cannot_construct_coordinator(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'MINDIE_AGENT_CONFIG':str(Path(tmp)/'absent.json')},clear=False):
            with patch('mindie_coordinator.task_client.TaskClient') as constructor:
                with self.assertRaises(TaskTargetError):task_client()
                constructor.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_deactivation_blocks_existing_client_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'codex.json';engine=Path(tmp)/'engine.json'
            engine.write_text('{}');config.write_text(json.dumps({'python':sys.executable,'engine_config':str(engine)}))
            with patch.dict(os.environ,{'MINDIE_AGENT_CONFIG':str(config),'CODEX_THREAD_ID':'controlled-component-admission'},clear=False):
                sessions=Sessions();lease=sessions.activate()
                with patch.dict(os.environ,{'MINDIE_SESSION_ID':lease['mindie_session_id'],'MINDIE_ACTIVATION':lease['mindie_activation']}),patch('mindie_coordinator.task_client.TaskClient') as constructor:
                    client=task_client();sessions.deactivate()
                    with self.assertRaises(Inactive):client.run('must not execute')
                    constructor.return_value.run.assert_not_called()
                    db=sessions.connect();self.assertEqual(db.execute('select count(*) from attempts').fetchone()[0],0);db.close()
