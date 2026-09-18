#!/usr/bin/env python3
"""Expose the core remote-dev tools from the local MindIE runtime."""

import json
import os
from pathlib import Path
import sys

from bridge import config_path

# Keep the upstream schemas, execution, ownership and result envelopes intact.
TOOLS = {
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


def main():
    config = json.loads(config_path().read_text())
    if sys.argv[1:] != ["--runtime"]:
        os.execv(
            config["python"],
            [config["python"], str(Path(__file__).absolute()), "--runtime"],
        )
    engine = json.loads(Path(config["engine_config"]).read_text())
    os.environ.setdefault(
        "REMOTE_DEV_STATE_DIR", str(Path(engine["root"]) / "remote-dev")
    )
    from remote_dev.mcp import server

    upstream_list, upstream_call = server.list_tools, server.call_tool
    server.list_tools = lambda: [
        tool for tool in upstream_list() if tool["name"] in TOOLS
    ]

    def call(name, arguments):
        if name not in TOOLS:
            raise ValueError("tool is outside this plugin's core remote-dev surface")
        return upstream_call(name, arguments)

    server.call_tool = call
    return server.main()


if __name__ == "__main__":
    raise SystemExit(main())
