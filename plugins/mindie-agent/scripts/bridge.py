#!/usr/bin/env python3
"""Codex plugin boundary. Hooks never start a service or persist retry work."""

import json
import os
from pathlib import Path
import subprocess
import sys


def config_path():
    return Path(
        os.environ.get(
            "MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json"
        )
    )


def main():
    operation = sys.argv[1]
    if operation == "session-start":
        try:
            event = json.load(sys.stdin)
            if config_path().is_file() and event.get("session_id"):
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "SessionStart",
                                "additionalContext": "MindIE Agent session_id="
                                + str(event["session_id"])
                                + ". Use this exact ID with MindIE knowledge tools only when selecting the configured domain.",
                            }
                        }
                    )
                )
            else:
                print("{}")
        except (ValueError, OSError):
            print("{}")
        return
    try:
        config = json.loads(config_path().read_text())
        command = [
            config["python"],
            "-m",
            "vaws_knowledge.loop.cli",
            "hook" if operation == "stop" else operation,
            "--config",
            config["engine_config"],
        ]
        if operation == "stop":
            subprocess.run(
                command,
                input=sys.stdin.read(),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            print("{}")
        else:
            os.execv(command[0], command)
    except (KeyError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        if operation == "stop":
            print("{}")
        else:
            print(
                "MindIE Agent is not configured: "
                + type(exc).__name__
                + ". Run scripts/setup.py with the knowledge runtime interpreter.",
                file=sys.stderr,
            )
            raise SystemExit(1)


if __name__ == "__main__":
    main()
