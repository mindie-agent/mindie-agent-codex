"""Live A2 (910B4) and A3 (910C) NPU cluster acceptance tests for Codex."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from tests.process_fixtures import (
    cleanup_temporary_directory,
    installed_scanner,
    public_engine_config,
    stop_owned_knowledge_service,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mcp_gate
import receipt_layout
import session_gate
from session_gate import Sessions

A2_ENDPOINT = {
    "host": "173.125.1.2",
    "port": 22,
    "user": "root",
    "container": "mindie-bench-a2-core-20261017",
}
A3_ENDPOINT = {
    "host": "192.168.13.165",
    "port": 22,
    "user": "root",
    "container": "vaws-offload-pd165-20261016",
}


def _iso_at(moment: float) -> str:
    whole = math.ceil(moment - 1e-6)
    return datetime.fromtimestamp(whole, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_before(moment: float, seconds: int) -> str:
    whole = math.floor(moment) - seconds
    return datetime.fromtimestamp(whole, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user_record(text: str, stamp: str) -> dict:
    return {
        "timestamp": stamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _assistant_record(text: str, stamp: str, phase: str = "final_answer") -> dict:
    return {
        "timestamp": stamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


class LiveNpuAcceptanceTests(unittest.TestCase):
    def setUp(self):
        real_home = Path.home()
        self.temp = tempfile.TemporaryDirectory(prefix="codex-live-npu-")
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.codex_home = self.root / "codex-home"
        self.diag_config = self.root / "diagnostics.json"
        self.diag_root = self.root / "diagnostics-state"
        self.remote_state = self.root / "remote-state"
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
        self.admission = self.root / "admission.sqlite3"
        self.community = self.root / "mindie-community.json"
        self.work = self.root / "work"
        for directory in (
            self.home,
            self.codex_home,
            self.diag_root,
            self.remote_state,
            self.work,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        real_ssh = real_home / ".ssh"
        if real_ssh.is_dir():
            os.symlink(real_ssh, self.home / ".ssh")
        self.engine_doc = public_engine_config(
            self.root / "data",
            adapter_profile=self.config,
            admission_path=str(self.admission),
            community_config=str(self.community),
        )
        self.engine.write_text(json.dumps(self.engine_doc), encoding="utf-8")
        self.adapter = {
            "python": sys.executable,
            "engine_config": str(self.engine),
            "community_config": str(self.community),
            "admission_path": str(self.admission),
            "runtime_scripts": str(SCRIPTS),
            "product_validation": self.engine_doc.get("product_validation"),
        }
        self.config.write_text(json.dumps(self.adapter), encoding="utf-8")
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_DATA_HOME": str(self.root / "xdg-data"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "CODEX_HOME": str(self.codex_home),
            "MINDIE_AGENT_CONFIG": str(self.config),
            "MINDIE_DIAGNOSTICS_CONFIG": str(self.diag_config),
            "MINDIE_DIAGNOSTICS_ROOT": str(self.diag_root),
            "MINDIE_REMOTE_STATE_DIR": str(self.remote_state),
            "CODEX_THREAD_ID": "live-npu-thread-1",
        }
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        self.env_patch = patch.dict(os.environ, env)
        self.env_patch.start()
        session_gate.bind_explicit_config(None)
        self.sessions = Sessions(self.config)

    def tearDown(self):
        try:
            stop_owned_knowledge_service(self.engine)
        finally:
            session_gate.bind_explicit_config(None)
            self.env_patch.stop()
            cleanup_temporary_directory(self.temp)

    def _remote_request(
        self,
        req_id: str,
        tool_name: str,
        arguments: dict,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
    ) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
                "_meta": {
                    "x-codex-turn-metadata": {
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                    },
                    "threadId": thread_id,
                },
            },
        }

    @staticmethod
    def _result_text(result: dict) -> str:
        return "\n".join(
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict)
        )

    def test_live_remote_mcp_gate_a2_and_a3_npu_inspection(self):
        gate = mcp_gate.Gate("remote")
        session_id = "sess-" + uuid.uuid4().hex[:12]
        thread_id = "thread-" + uuid.uuid4().hex[:12]
        turn_id = "turn-" + uuid.uuid4().hex[:12]
        remote_dir = f"/tmp/codex-live-test-{uuid.uuid4().hex[:12]}"
        a2_file = f"{remote_dir}/a2-npu.txt"
        a3_file = f"{remote_dir}/a3-npu.txt"

        try:
            a2_bash_req = self._remote_request(
                "req-a2-bash",
                "remote_bash",
                {
                    **A2_ENDPOINT,
                    "command": f"mkdir -p {remote_dir} && npu-smi info | tee {a2_file}",
                    "max_output_tokens": 4000,
                },
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            a2_bash = gate.call(a2_bash_req, timeout=45)
            self.assertFalse(a2_bash.get("isError"), a2_bash)
            a2_bash_text = self._result_text(a2_bash)
            self.assertIn("910B4", a2_bash_text)
            type(self)._live_a2_npu_text = a2_bash_text

            a2_read_req = self._remote_request(
                "req-a2-read",
                "remote_read",
                {
                    **A2_ENDPOINT,
                    "file_path": a2_file,
                },
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            a2_read = gate.call(a2_read_req, timeout=30)
            self.assertFalse(a2_read.get("isError"), a2_read)
            self.assertIn("910B4", self._result_text(a2_read))

            a3_bash_req = self._remote_request(
                "req-a3-bash",
                "remote_bash",
                {
                    **A3_ENDPOINT,
                    "command": f"mkdir -p {remote_dir} && npu-smi info | tee {a3_file}",
                    "max_output_tokens": 4000,
                },
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            a3_bash = gate.call(a3_bash_req, timeout=45)
            self.assertFalse(a3_bash.get("isError"), a3_bash)
            a3_bash_text = self._result_text(a3_bash)
            self.assertIn("Ascend910", a3_bash_text)
            self.assertIn("65536", a3_bash_text)
            self.assertIn("0000:83:00.0", a3_bash_text)
            type(self)._live_a3_npu_text = a3_bash_text

            a3_read_req = self._remote_request(
                "req-a3-read",
                "remote_read",
                {
                    **A3_ENDPOINT,
                    "file_path": a3_file,
                },
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            a3_read = gate.call(a3_read_req, timeout=30)
            self.assertFalse(a3_read.get("isError"), a3_read)
            a3_read_text = self._result_text(a3_read)
            self.assertIn("Ascend910", a3_read_text)
            self.assertIn("65536", a3_read_text)

            # Duplicate request identity must be rejected by RemoteReceipts without re-execution.
            duplicate = gate.call(a2_bash_req, timeout=15)
            self.assertTrue(duplicate.get("isError"), duplicate)
            self.assertIn("Duplicate MCP request", self._result_text(duplicate))

            receipt_db = (
                self.remote_state
                / f"gate-v{receipt_layout.FORMAT}"
                / f"{thread_id}.sqlite3"
            )
            self.assertTrue(receipt_db.is_file(), receipt_db)
            conn = sqlite3.connect(receipt_db)
            try:
                rows = conn.execute(
                    "SELECT identity, status FROM attempts ORDER BY started"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(status == "succeeded" for _, status in rows), rows)
        finally:
            for idx, endpoint in enumerate((A2_ENDPOINT, A3_ENDPOINT), start=1):
                cleanup_req = self._remote_request(
                    f"req-cleanup-{idx}",
                    "remote_bash",
                    {
                        **endpoint,
                        "command": f"rm -rf {remote_dir}",
                    },
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                gate.call(cleanup_req, timeout=30)

    def test_live_stop_entry_to_admission_and_redacted_capture(self):
        import consent
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.cli import load_transcript_adapter
        from mindie_knowledge.loop.engine import Engine
        from mindie_knowledge.loop.store import Store

        session_id = "live-capture-" + uuid.uuid4().hex[:10]
        turn_1 = "turn-a2-" + uuid.uuid4().hex[:8]
        turn_2 = "turn-a3-" + uuid.uuid4().hex[:8]

        a2_npu_text = getattr(type(self), "_live_a2_npu_text", None)
        a3_npu_text = getattr(type(self), "_live_a3_npu_text", None)
        if not a2_npu_text or not a3_npu_text:
            gate = mcp_gate.Gate("remote")
            a2_probe = gate.call(
                self._remote_request(
                    "probe-a2",
                    "remote_bash",
                    {**A2_ENDPOINT, "command": "npu-smi info", "max_output_tokens": 4000},
                    session_id=session_id,
                    thread_id=session_id,
                    turn_id=turn_1,
                ),
                timeout=45,
            )
            self.assertFalse(a2_probe.get("isError"), a2_probe)
            a2_npu_text = self._result_text(a2_probe)
            a3_probe = gate.call(
                self._remote_request(
                    "probe-a3",
                    "remote_bash",
                    {**A3_ENDPOINT, "command": "npu-smi info", "max_output_tokens": 4000},
                    session_id=session_id,
                    thread_id=session_id,
                    turn_id=turn_2,
                ),
                timeout=45,
            )
            self.assertFalse(a3_probe.get("isError"), a3_probe)
            a3_npu_text = self._result_text(a3_probe)

        self.assertIn("910B4", a2_npu_text)
        self.assertIn("Ascend910", a3_npu_text)
        self.assertIn("65536", a3_npu_text)

        # Configure real consent + community sharing + store + active lease.
        consent.record_choice("contribute", self.config)
        self.community.write_text(
            json.dumps(
                {
                    "schema": "mindie-community-config/1",
                    "enabled": True,
                    "generation": "g-live-npu",
                    "enabled_at": 1.0,
                    "repository": "mindie-agent/knowledge",
                    "branch": "main",
                    "project_roots": [str(self.work)],
                    "idle_seconds": 300,
                    "visibility": "public",
                    "consent_config": str(consent.consent_path_for(self.config)),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        store = Store(self.root / "data", "test")
        store.close()

        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": session_id}),
            patch.object(Path, "cwd", return_value=self.work),
        ):
            lease = self.sessions.activate()
        self.assertEqual(lease.get("mindie_session_id"), session_id)
        boundary = float(
            Admission(str(self.admission)).active_lease(session_id)["activated_at"]
        )

        transcript = self.codex_home / "sessions" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        meta_stamp = _iso_before(boundary, 5)
        turn1_stamp = _iso_at(boundary + 10)
        turn2_stamp = _iso_at(boundary + 20)
        secret_token = "sk-live-secret-token-1234567890abcdef"

        turn1_records = [
            {
                "type": "session_meta",
                "timestamp": meta_stamp,
                "payload": {
                    "id": session_id,
                    "cwd": str(self.work),
                    "timestamp": meta_stamp,
                },
            },
            _user_record(
                f"Inspect A2 NPU on root@173.125.1.2 with token {secret_token}",
                turn1_stamp,
            ),
            _assistant_record(
                f"A2 cluster NPU observation (910B4):\n{a2_npu_text[:1500]}",
                turn1_stamp,
            ),
        ]
        transcript.write_text(
            "".join(json.dumps(r) + "\n" for r in turn1_records),
            encoding="utf-8",
        )

        release_dir = self.root / "release"
        release_dir.mkdir(parents=True, exist_ok=True)
        (release_dir / "runtime.json").write_text(
            json.dumps({"python": sys.executable}),
            encoding="utf-8",
        )

        stop_env = dict(os.environ, CODEX_THREAD_ID=session_id)
        stop1_event = {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "turn_id": turn_1,
            "cwd": str(self.work),
            "transcript_path": str(transcript),
            "last_assistant_message": "A2 910B4 inspection complete",
        }
        stop1 = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "entry.py"),
                "--release",
                str(release_dir),
                "stop",
            ],
            input=json.dumps(stop1_event),
            text=True,
            capture_output=True,
            timeout=20,
            cwd=str(self.work),
            env=stop_env,
        )
        self.assertEqual(stop1.returncode, 0, stop1.stderr)

        # Append Turn 2 with A3 Ascend910 (16 Phy-ID) diagnostics and run incremental stop capture.
        turn2_records = [
            _user_record(
                "Now inspect A3 NPU on root@192.168.13.165",
                turn2_stamp,
            ),
            _assistant_record(
                f"A3 cluster NPU observation (Ascend910 16-Phy-ID):\n{a3_npu_text[:1500]}",
                turn2_stamp,
            ),
        ]
        with transcript.open("a", encoding="utf-8") as handle:
            for record in turn2_records:
                handle.write(json.dumps(record) + "\n")

        stop2_event = {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "turn_id": turn_2,
            "cwd": str(self.work),
            "transcript_path": str(transcript),
            "last_assistant_message": "A3 Ascend910 inspection complete",
        }
        stop2 = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "entry.py"),
                "--release",
                str(release_dir),
                "stop",
            ],
            input=json.dumps(stop2_event),
            text=True,
            capture_output=True,
            timeout=20,
            cwd=str(self.work),
            env=stop_env,
        )
        self.assertEqual(stop2.returncode, 0, stop2.stderr)

        # Drain queued captures through the real Engine + Rust privacy scanner.
        store = Store(self.root / "data", "test")
        try:
            engine = Engine(
                store,
                capture_mode="public-transcript",
                redactor_executable=installed_scanner(),
                summary_command=None,
                settings_path=str(self.community),
                admission=Admission(str(self.admission)),
                transcript_adapter=load_transcript_adapter(
                    {"transcript_adapter": str(SCRIPTS / "codex_transcript.py")}
                ),
            )
            rows = store.db.execute(
                "SELECT id, status FROM captures ORDER BY id"
            ).fetchall()
            self.assertGreaterEqual(len(rows), 1)
            for row_id, status in rows:
                if status in {"queued", "pending", "deferred"}:
                    engine._process(row_id)
            drafts = "\n".join(doc["content"] for doc in store.drafts_changed())
        finally:
            store.close()

        self.assertIn("910B4", drafts)
        self.assertIn("Ascend910", drafts)
        self.assertIn("65536", drafts)
        self.assertNotIn(secret_token, drafts)
        self.assertNotIn("173.125.1.2", drafts)
        self.assertNotIn("192.168.13.165", drafts)


if __name__ == "__main__":
    unittest.main()
