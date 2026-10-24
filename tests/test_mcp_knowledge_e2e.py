"""End-to-end MCP knowledge tool dispatch through real runtime_call.py and mindie_knowledge service.

No fake script doubles: Gate("knowledge") invokes the production runtime_call.py
helper, which connects to a real mindie_knowledge service backed by an isolated
SQLite store and verifies knowledge_query, knowledge_explain, and knowledge_feedback.
"""

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mindie_knowledge.loop.store import Store

from tests.process_fixtures import (
    cleanup_temporary_directory,
    public_engine_config,
    stop_owned_knowledge_service,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import bridge  # noqa: E402
import mcp_gate  # noqa: E402


class McpKnowledgeEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scope = self.root / "scope"
        self.scope.mkdir()
        self.codex_home = self.root / "codex-home"
        (self.codex_home / "sessions").mkdir(parents=True)
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
        self.community = self.root / "mindie-community.json"
        self.admission = self.root / "codex.admission.sqlite3"
        self.data_root = self.root / "data"

        self.engine.write_text(
            json.dumps(
                public_engine_config(
                    self.data_root,
                    domain="mindie",
                    adapter_profile=self.config,
                    admission_path=str(self.admission),
                    community_config=str(self.community),
                )
            ),
            encoding="utf-8",
        )
        self.config.write_text(
            json.dumps(
                dict(
                    python=sys.executable,
                    engine_config=str(self.engine),
                    community_config=str(self.community),
                    admission_path=str(self.admission),
                    runtime_scripts=str(SCRIPTS),
                )
            ),
            encoding="utf-8",
        )
        self.environment = patch.dict(
            os.environ,
            MINDIE_AGENT_CONFIG=str(self.config),
            CODEX_THREAD_ID="thread-e2e-1",
            CODEX_HOME=str(self.codex_home),
            MINDIE_DIAGNOSTICS_ROOT=str(self.root / "diagnostics"),
        )
        self.environment.start()
        # The production helper strips PYTHONPATH to use its installed pin.
        # This fixture's joint source carries the configured-domain router, so
        # keep it for the helper/service subprocesses.
        from session_gate import runtime_env as base_runtime_env
        def joint_env(config_file=None, *, adapter=None):
            value = base_runtime_env(config_file, adapter=adapter)
            value["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
            return value
        self.runtime_env_patch = patch.object(
            mcp_gate, "runtime_env", side_effect=joint_env)
        self.runtime_env_patch.start()
        self.addCleanup(self.runtime_env_patch.stop)
        self.admission_env_patch = patch("session_gate.runtime_env", side_effect=joint_env)
        self.admission_env_patch.start()
        self.addCleanup(self.admission_env_patch.stop)
        import consent
        consent.record_choice("disabled", self.config)
        self.community.write_text(
            json.dumps(
                dict(
                    schema="mindie-community-config/1",
                    enabled=False,
                    generation=None,
                    enabled_at=None,
                    repository=None,
                    branch="main",
                    project_roots=[],
                    idle_seconds=300,
                    consent_config=str(self.root / "mindie-consent.json"),
                )
            ),
            encoding="utf-8",
        )

        # Seed a real knowledge card into the local domain store before service start.
        store = Store(self.data_root, "mindie")
        try:
            self.seeded = store.create_draft(
                kind="experience",
                title="Ascend 910B HCCL ring initialization timeout",
                summary="Diagnose and resolve HCCL_CONNECT_TIMEOUT on Ascend 910B RoCEv2.",
                content=(
                    "Verify HCCL_IF_IP and ranktable device NIC mappings with "
                    "hccn_tool -i 0 -ip -g when HCCL ring initialization times out. 检查网卡映射。"
                ),
                conditions={"chip": "910b"},
            )
            store.advance_search_index()
        finally:
            store.close()

    def tearDown(self):
        try:
            stop_owned_knowledge_service(self.engine)
        finally:
            self.environment.stop()
            cleanup_temporary_directory(self.temp)

    def _call_mcp(self, gate, req_id, tool_name, arguments, *, thread_id="thread-e2e-1"):
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
                "_meta": {
                    "threadId": thread_id,
                    "sessionId": thread_id,
                    "x-codex-turn-metadata": {
                        "thread_id": thread_id,
                        "session_id": thread_id,
                        "turn_id": f"turn-{req_id}",
                    },
                },
            },
        }
        return gate.call(request)

    def test_mcp_knowledge_query_explain_feedback_end_to_end(self):
        gate = mcp_gate.Gate("knowledge")
        denied = self._call_mcp(gate, 0, "knowledge_query", {"query": "HCCL"})
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["code"], "knowledge_inactive")
        self.assertEqual(denied["structuredContent"]["execution"], "not_started")
        from manual_entry import authorize
        authorize(json.loads(self.config.read_text()), dict(
            hook_event_name="UserPromptSubmit", session_id="thread-e2e-1",
            cwd=str(self.scope), prompt="/mindie-agent"))
        with patch.object(Path, "cwd", return_value=self.scope):
            activated = bridge.activate("activate")
        self.assertEqual(activated["status"], "active")
        self.assertEqual(activated["mindie_session_id"], "thread-e2e-1")

        gate = mcp_gate.Gate("knowledge")

        # 1. knowledge_query
        query_res = self._call_mcp(
            gate,
            1,
            "knowledge_query",
            {"query": "HCCL ring initialization timeout", "limit": 5},
        )
        self.assertFalse(query_res.get("isError"), f"Unexpected query error: {query_res}")
        structured_query = query_res["structuredContent"]
        self.assertEqual(structured_query["domain"], "mindie")
        self.assertGreaterEqual(len(structured_query["results"]), 1)
        first_hit = structured_query["results"][0]
        self.assertEqual(first_hit["entry_id"], self.seeded["entry_id"])
        block_ref = first_hit["ref"]

        # A new native turn/connection in the same session needs no new entry.
        gate = mcp_gate.Gate("knowledge")

        # 2. knowledge_explain
        explain_res = self._call_mcp(
            gate,
            2,
            "knowledge_explain",
            {"ref": block_ref},
        )
        self.assertFalse(explain_res.get("isError"), f"Unexpected explain error: {explain_res}")
        structured_explain = explain_res["structuredContent"]
        self.assertEqual(structured_explain["entry_id"], self.seeded["entry_id"])
        self.assertIn("hccn_tool", structured_explain["content"])
        feedback_ref = structured_explain["feedback_ref"]

        # 3. knowledge_feedback
        feedback_res = self._call_mcp(
            gate,
            3,
            "knowledge_feedback",
            {
                "ref": feedback_ref,
                "rating": "up",
                "reason": "Verified HCCL NIC mapping fix on Ascend 910B.",
            },
        )
        self.assertFalse(feedback_res.get("isError"), f"Unexpected feedback error: {feedback_res}")
        structured_feedback = feedback_res["structuredContent"]
        self.assertEqual(structured_feedback["entry_id"], self.seeded["entry_id"])
        self.assertEqual(structured_feedback["rating"], "up")
        self.assertTrue(structured_feedback["vote_id"])
        gate.sessions.deactivate()
        denied = self._call_mcp(gate, 4, "knowledge_query", {"query": "HCCL"})
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["code"], "knowledge_inactive")

    def test_private_skill_entry_queries_and_reads_the_real_service(self):
        from knowledge_cli import invoke
        from manual_entry import authorize
        authorize(json.loads(self.config.read_text()), dict(
            hook_event_name="UserPromptSubmit", session_id="thread-e2e-1",
            cwd=str(self.scope), prompt="/mindie-agent"))
        query = invoke("knowledge_query", {"query": "HCCL ring initialization timeout", "limit": 5})
        self.assertFalse(query["isError"], query)
        first = query["structuredContent"]["results"][0]
        self.assertEqual(first["entry_id"], self.seeded["entry_id"])
        process = subprocess.run(
            [sys.executable, str(SCRIPTS / "knowledge_cli.py"), "knowledge_explain"],
            input=json.dumps({"ref": first["ref"]}).encode("utf-8"),
            capture_output=True, timeout=15,
            env=dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0"),
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        explain = json.loads(process.stdout)
        self.assertFalse(explain["isError"], explain)
        self.assertIn("hccn_tool", explain["structuredContent"]["content"])
        self.assertIn("检查网卡映射", explain["structuredContent"]["content"])


if __name__ == "__main__":
    unittest.main()
