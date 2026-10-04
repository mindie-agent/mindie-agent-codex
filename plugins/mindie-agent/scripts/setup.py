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
import consent
from runtime_probe import PROBE_MODULES
import product_contract
import sharing

SCRIPTS = Path(__file__).parent.absolute()

# Setup and updater use the same material-index and runtime API contract.


def probe_runtime(python):
    """Fail clearly, before any configuration write, when a pin is missing.

    Parser load goes through the configured interpreter's shared
    ``load_transcript_adapter`` (registers the module before exec). This
    process does not grow a second dynamic importer.
    """
    try:
        return product_contract.probe(
            python, SCRIPTS, lambda argv, **kwargs: run(argv, "", **kwargs).checked_stdout())
    except Exception as exc:
        raise SystemExit(
            f"knowledge runtime probe failed to run in {python}: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def replace_private(path, value):
    """Atomic rewrite of an existing MindIE JSON file (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".mindie-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def community_settings(args, parser, python, *, scripts=SCRIPTS):
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
    declaration, _ = product_contract.product(product_contract.source_root(scripts))
    publication = declaration["publication"]
    if settings["repository"] == publication["repository"]:
        settings["publication_contract_sha256"] = publication["contract_sha256"]
    try:
        structural = sharing.validate(settings)
        return sharing.normalize_with_runtime(structural, python)
    except sharing.SharingError as exc:
        parser.error(str(exc))


def interactive_choice(args, parser):
    """Confirm the supplied destination; there are no partial product modes."""
    print(sharing.CHOICES, file=sys.stderr)
    try:
        line = input("Configure the supplied public destination and scope? [yes/no]: ").strip()
    except EOFError:
        parser.error("interactive setup needs a response; configuration is incomplete")
    if line.lower() == "yes":
        if not (
            args.community_repository
            and args.community_project_root
            and args.community_visibility == "public"
        ):
            parser.error(
                "configuration requires --community-repository OWNER/REPO "
                "--community-project-root PATH --community-visibility public"
            )
        return "contribute"
    parser.error("configuration was not completed; no product mode was selected")


def install(args, parser):
    if not args.knowledge_python:
        parser.error("install requires --knowledge-python")
    python = str(Path(args.knowledge_python).expanduser().absolute())
    # Keep the venv executable path; resolving its symlink loses its site-packages.
    validation = probe_runtime(python)
    declaration, _ = product_contract.product(product_contract.source_root(SCRIPTS))
    transcript_adapter = str(SCRIPTS / "codex_transcript.py")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", args.domain):
        parser.error("invalid domain name")
    config = args.config.expanduser().absolute()
    engine_config = config.with_name(config.stem + ".engine.json")
    community_config = config.with_name("mindie-community.json")
    admission_path = config.with_name(config.stem + ".admission.sqlite3")
    if config.exists() or engine_config.exists() or community_config.exists():
        parser.error(
            "configuration already exists; use setup.py configure to record "
            "sharing on this installation, or choose --config"
        )
    # The profile consent authority is shared with every adapter in this
    # profile: an existing saved choice is reused and never re-asked or
    # overwritten by a fresh install. A damaged document is a fault to fix
    # first, never a state to silently clear.
    saved = consent.load(config)
    reused_choice = None
    if saved["state"] == "ok" and saved["choice"]:
        reused_choice = saved["choice"]
    elif saved["state"] in {"corrupt", "unreadable"}:
        parser.error(
            f"existing consent document is {saved['state']} "
            f"({saved['error']}); inspect and remove it explicitly before "
            "recording a new choice"
        )
    sharing_choice = None
    if args.interactive and reused_choice is None:
        sharing_choice = interactive_choice(args, parser)
    community = community_settings(args, parser, python)
    if community is not None:
        sharing_choice = "contribute"
    value = dict(
        root=str(args.root.expanduser().absolute()),
        domain=args.domain,
        admission_path=str(admission_path),
        transcript_adapter=transcript_adapter,
        community_config=str(community_config),
        product_validation=validation,
    )
    from capture_config import prepare
    value.update(prepare(python, SCRIPTS))
    if args.domain == declaration["publication"]["domain"] and not args.no_public_feed:
        value["feeds"] = [product_contract.publication_feed(declaration)]
    write_private(engine_config, value)
    adapter = dict(
        python=python,
        engine_config=str(engine_config),
        community_config=str(community_config),
        admission_path=str(admission_path),
        runtime_scripts=str(SCRIPTS),
        product_validation=validation,
    )
    write_private(config, adapter)
    if sharing_choice:
        # The explicit install-time choice lands directly in the consent
        # authority; the retired adapter-config sharing_choice key is never
        # written as a record.
        try:
            consent.record_choice(sharing_choice, config)
        except consent.ConsentError as exc:
            parser.error(str(exc))
    if community is not None:
        # The user explicitly selected repository, scope, account and
        # visibility: record them and enable sharing from this moment. Only
        # material authorized after enabled_at is ever captured. The consent
        # authority pointer wires the core gate to this profile's document.
        community["consent_config"] = str(consent.consent_path_for(config))
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
                reused_choice=reused_choice,
                next=(
                    None
                    if community is not None
                    else "sharing is off/unconfigured; first explicit invocation "
                    "asks only for missing destination and scope, or run scripts/setup.py configure "
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
    This explicit boundary also converges the community path onto the
    profile-shared authority and wires the consent authority pointer.
    """
    config = args.config.expanduser().absolute()
    if not config.is_file():
        parser.error(
            "no existing configuration; run setup.py install "
            "--knowledge-python PYTHON first"
        )
    adapter = json.loads(config.read_text(encoding='utf-8'))
    python = adapter.get("python")
    if not isinstance(python, str) or not python:
        parser.error("existing configuration has no runtime interpreter")
    scripts = adapter.get("runtime_scripts")
    if not isinstance(scripts, str) or not Path(scripts).is_absolute():
        parser.error("existing configuration has no selected product scripts")
    community = community_settings(args, parser, python, scripts=Path(scripts))
    if community is None:
        parser.error(
            "configure requires the explicit community selection "
            "(--community-repository, --community-project-root, "
            "--community-visibility public)"
        )
    # Validate the consent authority first, but do not claim completed setup
    # before the real configuration writes succeed.
    saved = consent.load(config)
    if saved["state"] in {"corrupt", "unreadable"}:
        parser.error("saved setup state is damaged; configuration was not changed")
    with sharing.community_write_lock(config):
        # One context for the whole boundary: converge adapter/engine/worker
        # onto the profile-shared path, then resolve and re-read the current
        # authority before writing.
        moved = sharing._migrate_community_locked(config)
        community_config = Path(moved["path"])
        # A damaged or non-conforming document fails here with its bytes
        # preserved — a managed mutation is never an implicit repair by
        # replacement. A parseable document is rewritten normally (explicit
        # re-selection is the repair path); a missing file is a first
        # configuration.
        try:
            raw = community_config.read_bytes()
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            parser.error(
                "community settings file is unreadable; inspect and repair "
                f"the designated file explicitly ({type(exc).__name__})"
            )
        extensions = {}
        old = {}
        if raw is not None:
            try:
                old = json.loads(raw)
            except ValueError:
                parser.error(
                    "community settings file is damaged; its bytes are "
                    "preserved. Inspect and remove it explicitly before "
                    "configuring again — a damaged authority is never "
                    "silently replaced."
                )
            if not isinstance(old, dict) or old.get("schema") != sharing.SCHEMA:
                parser.error(
                    "community settings file is not a valid "
                    "mindie-community-config/1 document; its bytes are "
                    "preserved. Inspect and remove it explicitly before "
                    "configuring again."
                )
            extensions = {
                key: value
                for key, value in old.items()
                if key not in sharing.CORE_KEYS and key != "consent_config"
                and (key != "publication_contract_sha256"
                     or old.get("repository") == community["repository"])
            }
        merged = sharing.normalize_with_runtime(
            {**extensions, **community}, python, config, previous=old
        )
        merged["consent_config"] = str(consent.consent_path_for(config))
        community_config.parent.mkdir(parents=True, exist_ok=True)
        if merged != old:
            sharing.write(community_config, merged)
    adapter["community_config"] = str(community_config)
    adapter.pop("sharing_choice", None)  # retired migration source
    adapter.pop("session_activation", None)
    engine_path = Path(adapter.get("engine_config") or "")
    if engine_path.is_file():
        try:
            engine = json.loads(engine_path.read_text(encoding='utf-8'))
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
    consent.record_choice("contribute", config)
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
