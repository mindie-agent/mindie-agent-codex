"""The public helper may exit; a prepared service and a Stop wake must survive.

Real adapter dispatch, core processes/locks/database. Only the organizer is
a deterministic local program; no native model or public transport is used.
"""
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

from tests.test_sharing import SharingFixture, SCRIPTS
from tests.process_fixtures import stop_owned_knowledge_service
from mindie_knowledge.loop.cli import connect, rpc


class ServiceEntryLifetimeTests(SharingFixture):
    def bridge(self, operation, *, event=None):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), "--config", str(self.config), operation],
            input=json.dumps(event) if event else "", text=True, encoding="utf-8",
            capture_output=True, cwd=self.scope, timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_activation_and_cold_stop_survive_short_helper_exit(self):
        import consent

        self.write_sharing()
        consent.record_choice("contribute")
        engine = json.loads(self.engine.read_text())
        from tests.test_parallel_codex_contract import installed_scanner
        engine.pop("agent_command", None)
        engine.update(capture_mode="public-transcript", redactor_executable=installed_scanner(),
                      transcript_adapter=str(SCRIPTS / "codex_transcript.py"))
        self.engine.write_text(json.dumps(engine))
        result = self.bridge("activate")
        self.assertEqual(result["experience"], "capture-ready")
        # The bridge AND bounded runtime helper have exited before this probe.
        self.assertTrue(rpc(connect(engine), "status", timeout=1)["worker_alive"])
        stop_owned_knowledge_service(self.engine)
        from datetime import datetime, timezone
        transcript = self.root / "public.jsonl"
        transcript.write_text(json.dumps(dict(type="session_meta", payload=dict(id="manual-A"))) + "\n" +
            json.dumps(dict(type="response_item", timestamp=datetime.now(timezone.utc).isoformat(),
                payload=dict(type="message", role="assistant", phase="final_answer",
                    content=[dict(type="output_text", text="Synthetic lifetime test: public body persisted.")]))) + "\n", encoding="utf-8")
        self.bridge("stop", event=self.event(transcript_path=str(transcript)))
        path = self.root / "data/test/store-v3.sqlite3"
        deadline = time.monotonic() + 8
        statuses = []
        while time.monotonic() < deadline:
            with closing(sqlite3.connect(path)) as db:
                statuses = [r[0] for r in db.execute("SELECT status FROM captures")]
            if statuses == ["organized"]:
                break
            time.sleep(.05)
        if statuses != ["organized"]:
            from mindie_knowledge.loop.diagnostics import snapshot
            view = snapshot(self.engine, session="manual-A")
            wake_path = path.with_name("wake.json")
            wake = json.loads(wake_path.read_text()) if wake_path.exists() else None
            self.fail(json.dumps(dict(statuses=statuses, startup=view["startup"],
                                      delivery=view["delivery"], service=view["service"], wake=wake)))
        self.assertTrue(rpc(connect(engine), "status", timeout=1)["worker_alive"])
