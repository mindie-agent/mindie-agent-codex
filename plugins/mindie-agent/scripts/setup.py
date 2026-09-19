#!/usr/bin/env python3
"""Configure only MindIE-owned files; install/trust the plugin through Codex."""

import argparse
import json
import os
from pathlib import Path
import sys

from bounded_process import run

# The configured interpreter must carry the exact runtime and domain pins;
# the updater's capability probe covers deeper runtime behavior.
PROBE_MODULES = (
    "mindie_knowledge.loop.cli",
    "knowledge_intake",
    "remote_dev.mcp.server",
    "mindie_coordinator.task_client",
)
PROBE_TIMEOUT = 15

PROBE_SCRIPT = """
import importlib
missing = []
for name in {modules!r}:
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{{name}} ({{type(exc).__name__}}: {{exc}})")
print("MISSING: " + "; ".join(missing) if missing else "OK")
""".format(modules=list(PROBE_MODULES))


def probe_runtime(python):
    """Fail clearly, before any configuration write, when a pin is missing.

    One bounded child with owned process-group cleanup; no retry. A broken
    interpreter (timeout/nonzero exit) and a missing module both stop setup.
    """
    try:
        output = run([python, "-c", PROBE_SCRIPT], "", timeout=PROBE_TIMEOUT)
    except Exception as exc:
        raise SystemExit(
            f"knowledge runtime probe failed to run in {python}: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
    if not output.strip().endswith("OK"):
        raise SystemExit(
            f"{python} is missing pinned dependencies: {output.strip()}. "
            "Install runtime-requirements.txt and domain-requirements.txt first."
        )


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--knowledge-python",
        required=True,
        help="Python 3.11+ interpreter with the MindIE knowledge runtime installed",
    )
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/mindie-agent/codex.json"
    )
    parser.add_argument(
        "--root", type=Path, default=Path.home() / ".local/share/mindie-agent"
    )
    parser.add_argument("--domain", default="vllm-ascend")
    parser.add_argument(
        "--no-public-feed",
        action="store_true",
        help="Do not read the official vLLM-Ascend domain feed",
    )
    parser.add_argument(
        "--upstream",
        type=Path,
        help="Explicitly share completed use evidence with this service connection JSON",
    )
    parser.add_argument(
        "--auto-publish",
        action="store_true",
        help="Authorize sanitized organized entries for distribution to configured peers",
    )
    args = parser.parse_args()
    python = str(Path(args.knowledge_python).expanduser().absolute())
    # Keep the venv executable path; resolving its symlink loses its site-packages.
    probe_runtime(python)
    import re

    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", args.domain):
        parser.error("invalid domain name")
    config = args.config.expanduser().absolute()
    engine_config = config.with_name(config.stem + ".engine.json")
    if config.exists() or engine_config.exists():
        parser.error(
            "configuration already exists; edit the MindIE files explicitly or choose --config"
        )
    value = dict(
        root=str(args.root.expanduser().absolute()),
        domain=args.domain,
        agent_command=[
            sys.executable,
            str(Path(__file__).with_name("agent_worker.py").absolute()),
        ],
        auto_publish=args.auto_publish,
        session_activation=str(config),
    )
    if args.upstream:
        value["upstream"] = json.loads(args.upstream.read_text())
    if args.domain == "vllm-ascend" and not args.no_public_feed:
        value["feeds"] = [
            dict(
                repository="mindie-agent/knowledge",
                ref="knowledge/vllm-ascend",
                domain="vllm-ascend",
                interval_seconds=300,
            )
        ]
    write_private(engine_config, value)
    write_private(config, dict(python=python, engine_config=str(engine_config)))
    print(
        json.dumps(
            dict(
                config=str(config), engine_config=str(engine_config), domain=args.domain
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
