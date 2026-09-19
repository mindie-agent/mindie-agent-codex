"""Build-time schema snapshot; discovery must not import the business runtime."""

import copy
import json
from pathlib import Path

REMOTE_TOOLS = {
    "remote_bash",
    "remote_read",
    "remote_write",
    "remote_grep",
    "remote_apply_patch",
    "remote_job_status",
    "remote_job_tail",
    "remote_job_stdin",
    "remote_job_stop",
    "remote_artifact_pull",
    "remote_artifact_push",
}


def catalog():
    from mindie_knowledge.loop.cli import TOOLS
    from remote_dev.mcp.tools import list_tools

    result = dict(
        knowledge=copy.deepcopy(TOOLS),
        remote=[copy.deepcopy(t) for t in list_tools() if t["name"] in REMOTE_TOOLS],
    )
    for tools in result.values():
        for tool in tools:
            tool["description"] = (
                "Requires manual MindIE activation in this session. "
                + tool["description"]
            )
            schema = tool["inputSchema"]
            for key, text in [
                (
                    "mindie_session_id",
                    "Native Codex session ID from manual activation; not a remote job ID",
                ),
                (
                    "mindie_activation",
                    "Session capability from manual activation; never copy from another session",
                ),
            ]:
                schema["properties"][key] = dict(type="string", description=text)
                schema.setdefault("required", []).append(key)
    return result


if __name__ == "__main__":
    Path(__file__).with_name("mcp_catalog.json").write_text(
        json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n"
    )
