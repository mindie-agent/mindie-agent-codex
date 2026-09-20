#!/usr/bin/env python3
"""Configure only MindIE-owned files; install/trust the plugin through Codex.

Plugin activation and community sharing are separate: activation/config works
without any sharing setup, and community settings (repository, scope, account,
visibility) are recorded only when explicitly selected via --community-*.
Community sharing defaults OFF; it can later be toggled with
bridge.py sharing-enable / sharing-disable / sharing-status.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

from bounded_process import run
import sharing

# The configured interpreter must carry the exact runtime and domain pins,
# matching the actual new package APIs; the updater's capability probe covers
# deeper runtime behavior.
PROBE_MODULES = (
    "mindie_knowledge.loop.cli",
    "mindie_knowledge.loop.documents",
    "mindie_knowledge.loop.transcript",
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


def community_settings(args, parser):
    """Explicitly selected sharing settings; None leaves sharing OFF."""
    selected = any(
        getattr(args, key)
        for key in (
            "community_repository",
            "community_project_root",
            "community_account",
            "community_fork",
            "community_bot",
            "community_visibility",
        )
    )
    if not selected:
        if args.community_branch is not None:
            parser.error("--community-branch requires --community-repository")
        return None
    if not args.community_repository:
        parser.error("community sharing requires --community-repository owner/repo")
    if not args.community_project_root:
        parser.error("community sharing requires at least one --community-project-root")
    if args.community_visibility != "public":
        parser.error(
            "community sharing requires --community-visibility public; "
            "no other visibility is supported"
        )
    roots = []
    for root in args.community_project_root:
        canonical = sharing.canonical_root(str(Path(root).expanduser().absolute()))
        if not Path(canonical).is_dir():
            parser.error("community project root does not exist: " + canonical)
        if canonical not in roots:
            roots.append(canonical)
    settings = dict(
        schema=sharing.SCHEMA,
        enabled=True,
        generation=secrets.token_hex(16),
        enabled_at=time.time(),
        repository=args.community_repository,
        branch=args.community_branch or "main",
        project_roots=roots,
        idle_seconds=300,
    )
    for key in ("fork", "account"):
        value = getattr(args, "community_" + key)
        if value:
            settings[key] = value
    if args.community_bot:
        settings["bot"] = {"account": args.community_bot}
    settings["visibility"] = args.community_visibility
    try:
        return sharing.validate(settings)
    except sharing.SharingError as exc:
        parser.error(str(exc))


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
        "--community-repository",
        metavar="OWNER/REPO",
        help="Explicitly enable community sharing against this content repository",
    )
    parser.add_argument(
        "--community-project-root",
        action="append",
        metavar="PATH",
        help="Authorized local task scope; repeatable",
    )
    parser.add_argument("--community-branch", help="Content branch (default main)")
    parser.add_argument(
        "--community-account", help="Explicit publishing account name (no tokens)"
    )
    parser.add_argument(
        "--community-fork",
        metavar="OWNER/REPO",
        help="Contributor-controlled fork for contribution branches",
    )
    parser.add_argument("--community-bot", help="Approved review bot target")
    parser.add_argument(
        "--community-visibility",
        choices=["public"],
        help="Required with community sharing: contributions are public",
    )
    args = parser.parse_args()
    python = str(Path(args.knowledge_python).expanduser().absolute())
    # Keep the venv executable path; resolving its symlink loses its site-packages.
    probe_runtime(python)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", args.domain):
        parser.error("invalid domain name")
    community = community_settings(args, parser)
    config = args.config.expanduser().absolute()
    engine_config = config.with_name(config.stem + ".engine.json")
    community_config = config.with_name(config.stem + ".community.json")
    if config.exists() or engine_config.exists() or community_config.exists():
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
        session_activation=str(config),
        community_config=str(community_config),
    )
    if args.domain == "vllm-ascend" and not args.no_public_feed:
        value["feeds"] = [
            dict(
                repository="mindie-agent/knowledge-vllm-ascend",
                ref="main",
                domain="vllm-ascend",
                interval_seconds=300,
            )
        ]
    write_private(engine_config, value)
    write_private(
        config,
        dict(
            python=python,
            engine_config=str(engine_config),
            community_config=str(community_config),
        ),
    )
    if community is not None:
        # The user explicitly selected repository, scope, account and
        # visibility: record them and enable sharing from this moment. Only
        # material authorized after enabled_at is ever captured.
        sharing.write(community_config, community)
    print(
        json.dumps(
            dict(
                config=str(config),
                engine_config=str(engine_config),
                community_config=str(community_config),
                domain=args.domain,
                sharing="enabled" if community is not None else "off",
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
