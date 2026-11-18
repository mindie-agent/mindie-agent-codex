"""Exercise default-on MCP discovery and CLI catalog inspection without starting services."""
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from mindie_knowledge.loop.activation import Admission

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import knowledge_cli
import mcp_gate
import native_entry


def discover(surface):
    incoming = io.TextIOWrapper(io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'))
    outgoing = io.TextIOWrapper(io.BytesIO())
    with patch.object(sys, "stdin", incoming), patch.object(sys, "stdout", outgoing), \
            patch("update_trigger.Maintenance"), \
            patch("importlib.metadata.version", return_value="test"):
        mcp_gate.serve(surface)
        outgoing.flush()
        return json.loads(outgoing.buffer.getvalue())["result"]


def test_public_wire_discovery_never_leaks_another_chats_knowledge(tmp_path, monkeypatch):
    config = tmp_path / "adapter.json"
    admission = tmp_path / "admission.sqlite3"
    config.write_text(json.dumps(dict(admission_path=str(admission))))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    authority = Admission(admission)
    expected_knowledge = {
        "knowledge_query",
        "knowledge_explain",
        "knowledge_feedback",
        "knowledge_distill",
        "knowledge_sync",
    }
    with patch("service_client.connect", side_effect=AssertionError("discovery started knowledge")):
        know_tools = discover("knowledge")["tools"]
        assert {t["name"] for t in know_tools} == expected_knowledge
        for ro_name in ("knowledge_query", "knowledge_explain"):
            tool_def = next(t for t in know_tools if t["name"] == ro_name)
            assert tool_def["annotations"]["readOnlyHint"] is True

        remote_tools = discover("remote")["tools"]
        assert remote_tools
        assert all(not tool["name"].startswith("knowledge_") for tool in remote_tools)

        comp_tools = discover("components")["tools"]
        comp_names = {t["name"] for t in comp_tools}
        assert expected_knowledge <= comp_names
        assert {"diagnostics_status", "diagnostics_bundle", "debugger", "coordinator",
                "profiler", "sim", "npu_top", "rsi_loop", "experiment_ledger"} <= comp_names
        assert not admission.exists()

        monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
        schemas = knowledge_cli.invoke("describe", {})["tools"]
        assert {tool["name"] for tool in schemas} == expected_knowledge
        assert not admission.exists()

        authority.activate("muted-chat", project_root=str(tmp_path))
        authority.deactivate("muted-chat")
        monkeypatch.setenv("CODEX_THREAD_ID", "muted-chat")
        with pytest.raises(knowledge_cli.KnowledgeInactive):
            knowledge_cli.invoke("describe", {})


def test_manifest_routes_every_public_server_to_nonknowledge_discovery(tmp_path):
    (tmp_path / ".codex-plugin").mkdir()
    (tmp_path / "hooks").mkdir()
    (tmp_path / ".codex-plugin/plugin.json").write_text('{"version":"test"}')
    native_entry.write_manifests(tmp_path)
    servers = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    roles = {"remote-mcp": "remote", "tools-mcp": "components"}
    visible = []
    for server in servers.values():
        visible.extend(discover(roles[server["args"][-1]])["tools"])
    names = {tool["name"] for tool in visible}
    assert {
        "remote_bash",
        "profiler",
        "npu_top",
        "rsi_loop",
        "experiment_ledger",
        "knowledge_query",
        "knowledge_explain",
        "knowledge_feedback",
        "knowledge_distill",
        "knowledge_sync",
    } <= names
