"""The private CLI starts under isolated Python and retains its manual gate."""
import json
import os
from pathlib import Path
import subprocess
import sys

from mindie_knowledge.loop.activation import Admission


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


def test_isolated_describe_requires_the_current_session_grant(tmp_path):
    admission = tmp_path / "admission.sqlite3"
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(admission_path=str(admission))))
    environment = dict(os.environ, MINDIE_AGENT_CONFIG=str(config),
                       CODEX_THREAD_ID="manual-cli")
    command = [sys.executable, "-I", str(SCRIPTS / "knowledge_cli.py"), "describe"]

    denied = subprocess.run(command, cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=5)
    assert denied.returncode == 1, denied.stderr
    assert json.loads(denied.stdout)["isError"] is True
    assert "tools" not in json.loads(denied.stdout)
    gate = Admission(admission)
    assert gate.knowledge_lease("manual-cli") is None

    gate.activate("manual-cli", project_root=str(tmp_path))
    allowed = subprocess.run(command, cwd=tmp_path, env=environment,
                             capture_output=True, text=True, timeout=5)
    assert allowed.returncode == 0, allowed.stderr
    tools = json.loads(allowed.stdout)["tools"]
    explain = next(tool for tool in tools if tool["name"] == "knowledge_explain")
    assert "refs" in explain["inputSchema"]["properties"]
    assert gate.knowledge_lease("manual-cli") is not None
