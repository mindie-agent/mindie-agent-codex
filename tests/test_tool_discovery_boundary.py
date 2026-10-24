"""Exercise MCP discovery and explicit private entry without starting services."""
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
    with patch("service_client.connect", side_effect=AssertionError("discovery started knowledge")):
        assert discover("knowledge") == {"tools": []}
        for surface in ("remote", "components"):
            descriptions = discover(surface)["tools"]
            assert descriptions
            # Inspect the actual response, not just whether calls are denied.
            assert all(not tool["name"].startswith("knowledge_") for tool in descriptions)
        assert not admission.exists()
        authority.activate("enabled-chat", project_root=str(tmp_path))
        monkeypatch.setenv("CODEX_THREAD_ID", "ordinary-chat")
        assert discover("knowledge") == {"tools": []}
        with pytest.raises(knowledge_cli.KnowledgeInactive):
            knowledge_cli.invoke("describe", {})
        monkeypatch.setenv("CODEX_THREAD_ID", "enabled-chat")
        schemas = knowledge_cli.invoke("describe", {})["tools"]
        assert any(tool["name"] == "knowledge_query" for tool in schemas)
        # Even an enabled thread never changes shared MCP discovery.
        assert discover("knowledge") == {"tools": []}
        authority.deactivate("enabled-chat")
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
    assert any(tool["name"] == "remote_bash" for tool in visible)
    assert any(tool["name"] == "profiler" for tool in visible)
    assert all(not tool["name"].startswith("knowledge_") for tool in visible)
    assert all("npu_top" not in tool["name"] for tool in visible)
