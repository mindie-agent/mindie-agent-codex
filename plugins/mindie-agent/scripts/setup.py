#!/usr/bin/env python3
"""Configure only MindIE-owned files; install/trust the plugin through Codex.

Plugin activation and community sharing are separate: activation/config works
without any sharing setup, and community settings (repository, scope, account,
visibility) are recorded only when explicitly selected via --community-*.
Community sharing defaults OFF; after installation it is configured with the
supported `configure` operation (which never refuses merely because the
engine configuration already exists) and toggled with
bridge.py sharing-enable / sharing-disable / sharing-status.

The engine configuration records the explicit neutral admission database
(`admission_path`) and the adapter-owned transcript parser
(`transcript_adapter`, the installed codex_transcript.py exporting
FileIdentity/identify/read_material); the adapter configuration additionally
records the committed generation's scripts directory (`runtime_scripts`).
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

from bounded_process import run
import sharing

SCRIPTS = Path(__file__).parent.absolute()

# The configured interpreter must carry the exact runtime pins,
# matching the actual new package APIs; the updater's capability probe covers
# deeper runtime behavior.
PROBE_MODULES = (
    "mindie_knowledge.loop.cli",
    "mindie_knowledge.loop.documents",
    "mindie_knowledge.loop.activation",
    "remote_dev.mcp.server",
)
PROBE_TIMEOUT = 15

ADMISSION_METHODS = ("activate", "check", "resolve", "claim", "finish", "deactivate")


def probe_runtime(python):
    """Fail clearly, before any configuration write, when a pin is missing.

    Parser load goes through the configured interpreter's shared
    ``load_transcript_adapter`` (registers the module before exec). This
    process does not grow a second dynamic importer.
    """
    adapter = str(SCRIPTS / "codex_transcript.py")
    script = (
        "import importlib, inspect\n"
        "missing = []\n"
        f"for name in {list(PROBE_MODULES)!r}:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        missing.append(f'{name} ({type(exc).__name__}: {exc})')\n"
        "if not missing:\n"
        "    from mindie_knowledge.loop.activation import Admission\n"
        "    from mindie_knowledge.loop.cli import load_transcript_adapter\n"
        "    if 'path' not in inspect.signature(Admission.__init__).parameters:\n"
        "        missing.append('Admission does not take an explicit admission path')\n"
        f"    for method in {ADMISSION_METHODS!r}:\n"
        "        if not hasattr(Admission, method):\n"
        "            missing.append('Admission lacks ' + method)\n"
        f"    adapter = {adapter!r}\n"
        "    try:\n"
        "        module = load_transcript_adapter({'transcript_adapter': adapter})\n"
        "        if module is None:\n"
        "            missing.append('load_transcript_adapter returned None')\n"
        "        else:\n"
        "            for name in ('FileIdentity', 'identify', 'read_material'):\n"
        "                if not hasattr(module, name):\n"
        "                    missing.append('transcript adapter lacks ' + name)\n"
        "    except Exception as exc:\n"
        "        missing.append(f'transcript adapter ({type(exc).__name__}: {exc})')\n"
        "print('MISSING: ' + '; '.join(missing) if missing else 'OK')\n"
    )
    try:
        output = run([python, "-c", script], "", timeout=PROBE_TIMEOUT)
    except Exception as exc:
        raise SystemExit(
            f"knowledge runtime probe failed to run in {python}: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
    if not output.strip().endswith("OK"):
        raise SystemExit(
            f"{python} is missing pinned dependencies: {output.strip()}. "
            "Install runtime-requirements.txt first."
        )


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def replace_private(path, value):
    """Atomic rewrite of an existing MindIE JSON file (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".mindie-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def community_settings(args, parser, python):
    """Explicitly selected sharing settings; None leaves sharing OFF."""
    selected = any(
        getattr(args, key)
        for key in (
            "community_repository",
            "community_project_root",
            "community_account",
            "community_fork",
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
    settings["visibility"] = args.community_visibility
    try:
        structural = sharing.validate(settings)
        return sharing.normalize_with_runtime(structural, python)
    except sharing.SharingError as exc:
        parser.error(str(exc))


def interactive_choice(args, parser):
    """Optional installer prompt. No default yes; headless never calls this."""
    print(sharing.CHOICES, file=sys.stderr)
    try:
        line = input("Choice [1/2/3]: ").strip()
    except EOFError:
        parser.error("interactive setup requires an explicit 1, 2 or 3; no default")
    if line == "1":
        if not (
            args.community_repository
            and args.community_project_root
            and args.community_visibility == "public"
        ):
            parser.error(
                "choice 1 requires --community-repository OWNER/REPO "
                "--community-project-root PATH --community-visibility public"
            )
        return "contribute"
    if line == "2":
        return "read-only"
    if line == "3":
        return "later"
    parser.error("no default yes; choose 1, 2 or 3")


def install(args, parser):
    if not args.knowledge_python:
        parser.error("install requires --knowledge-python")
    python = str(Path(args.knowledge_python).expanduser().absolute())
    # Keep the venv executable path; resolving its symlink loses its site-packages.
    probe_runtime(python)
    transcript_adapter = str(SCRIPTS / "codex_transcript.py")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", args.domain):
        parser.error("invalid domain name")
    sharing_choice = None
    if args.interactive:
        sharing_choice = interactive_choice(args, parser)
    community = community_settings(args, parser, python)
    if community is not None:
        sharing_choice = "contribute"
    config = args.config.expanduser().absolute()
    engine_config = config.with_name(config.stem + ".engine.json")
    community_config = config.with_name(config.stem + ".community.json")
    admission_path = config.with_name(config.stem + ".admission.sqlite3")
    if config.exists() or engine_config.exists() or community_config.exists():
        parser.error(
            "configuration already exists; use setup.py configure to record "
            "sharing on this installation, or choose --config"
        )
    value = dict(
        root=str(args.root.expanduser().absolute()),
        domain=args.domain,
        agent_command=[
            python,
            str(SCRIPTS / "agent_worker.py"),
        ],
        admission_path=str(admission_path),
        transcript_adapter=transcript_adapter,
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
    adapter = dict(
        python=python,
        engine_config=str(engine_config),
        community_config=str(community_config),
        admission_path=str(admission_path),
        runtime_scripts=str(SCRIPTS),
    )
    if sharing_choice:
        adapter["sharing_choice"] = sharing_choice
    write_private(config, adapter)
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
                admission_path=str(admission_path),
                transcript_adapter=transcript_adapter,
                domain=args.domain,
                sharing="enabled" if community is not None else "off",
                sharing_choice=sharing_choice,
                next=(
                    None
                    if community is not None
                    else "sharing is off/unconfigured; first explicit invocation "
                    "offers three choices, or run scripts/setup.py configure "
                    "--community-repository OWNER/REPO "
                    "--community-project-root PATH --community-visibility public"
                ),
            ),
            indent=2,
        )
    )


def configure(args, parser):
    """Supported post-install sharing configuration.

    Works on an existing installation by design: it never refuses merely
    because the engine configuration already exists. Enabling requires the
    full explicit selection (repository, scope, public visibility); sibling
    components' extension keys in an existing settings file are preserved.
    """
    config = args.config.expanduser().absolute()
    if not config.is_file():
        parser.error(
            "no existing configuration; run setup.py install "
            "--knowledge-python PYTHON first"
        )
    adapter = json.loads(config.read_text())
    python = adapter.get("python")
    if not isinstance(python, str) or not python:
        parser.error("existing configuration has no runtime interpreter")
    community = community_settings(args, parser, python)
    if community is None:
        parser.error(
            "configure requires the explicit community selection "
            "(--community-repository, --community-project-root, "
            "--community-visibility public)"
        )
    community_config = Path(
        adapter.get("community_config")
        or config.with_name(config.stem + ".community.json")
    )
    previous = {}
    try:
        old = json.loads(community_config.read_text())
        if isinstance(old, dict):
            previous = {
                key: value
                for key, value in old.items()
                if key not in sharing.CORE_KEYS
            }
    except (OSError, ValueError):
        pass
    merged = sharing.normalize_with_runtime(
        {**previous, **community}, python, config
    )
    community_config.parent.mkdir(parents=True, exist_ok=True)
    sharing.write(community_config, merged)
    adapter["community_config"] = str(community_config)
    adapter["sharing_choice"] = "contribute"
    adapter.pop("session_activation", None)
    engine_path = Path(adapter.get("engine_config") or "")
    if engine_path.is_file():
        try:
            engine = json.loads(engine_path.read_text())
        except (OSError, ValueError):
            engine = None
        if isinstance(engine, dict) and "session_activation" in engine:
            engine.pop("session_activation", None)
            if "admission_path" not in engine and isinstance(
                adapter.get("admission_path"), str
            ):
                engine["admission_path"] = adapter["admission_path"]
            replace_private(engine_path, engine)
    replace_private(config, adapter)
    print(
        json.dumps(
            dict(
                community_config=str(community_config),
                sharing="enabled",
                sharing_choice="contribute",
                repository=merged["repository"],
                project_roots=merged["project_roots"],
                enabled_at=merged["enabled_at"],
                note="only material authorized after enabled_at is captured; "
                "toggle with bridge.py sharing-enable / sharing-disable. "
                "Existing engine configuration was left in place.",
            ),
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        nargs="?",
        default="install",
        choices=["install", "configure"],
        help="install writes a fresh configuration; configure records sharing "
        "settings on an existing installation",
    )
    parser.add_argument(
        "--knowledge-python",
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
    parser.add_argument(
        "--community-visibility",
        choices=["public"],
        help="Required with community sharing: contributions are public",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Offer the three first-use sharing choices; never default yes. "
        "Headless install (the default) leaves sharing unconfigured/off.",
    )
    args = parser.parse_args()
    if args.operation == "configure":
        configure(args, parser)
    else:
        install(args, parser)


if __name__ == "__main__":
    main()
