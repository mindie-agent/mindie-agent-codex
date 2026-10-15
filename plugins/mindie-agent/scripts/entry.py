"""Execute this installed release; no native mutation or current-generation selector."""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import runpy
import sys

# All native entries use -I and this exact installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import product_contract
from diagnostic_fallback import _private_mkdir
from session_gate import bind_explicit_config, runtime_binding
from setup import install
from update_lock import update_lock
SCRIPTS = Path(__file__).resolve().parent


def read(path):
    return product_contract.read_json(path)[0]


class ProfileUnavailable(ValueError):
    """Static caller guidance for an unsupported or replaced installation."""


def _installed_profile(config, receipt):
    adapter = read(config)
    if (adapter.get("python") != sys.executable
            or adapter.get("runtime_scripts") != str(SCRIPTS)
            or adapter.get("product_validation") != receipt["validation"]):
        raise ProfileUnavailable(
            "unsupported_development_replacement: this runtime cannot replace the "
            "existing installation; its configuration, data and consent are preserved."
        )
    engine = Path(adapter["engine_config"])
    value = read(engine)
    if ("client_contract" in value
            or value.get("transcript_adapter") != str(SCRIPTS / "codex_transcript.py")
            or value.get("product_validation") != receipt["validation"]):
        raise ProfileUnavailable("unsupported development profile; preserve its data and explicit consent; finish installation before using this runtime")
    return config


def profile(release, source=None, *, prepare=True):
    receipt = read(release / "runtime.json")
    if receipt["python"] != sys.executable:
        raise ValueError("release interpreter differs from its prepared runtime")
    config = Path(os.environ.get("MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json")).expanduser().absolute()
    if os.path.lexists(config):
        return _installed_profile(config, receipt)
    if not prepare:
        raise RuntimeError("MindIE release profile is not prepared; start a new native session")
    if source is not None and source.is_dir():
        product_contract.validate_receipt(json.dumps(receipt["validation"]), product_contract.identity(source))
    _private_mkdir(config.parent, parents=True, exist_ok=True)
    with update_lock(config, exclusive=True, blocking=True):
        if not os.path.lexists(config):
            args = argparse.Namespace(knowledge_python=sys.executable, config=config,
                root=Path.home() / ".local/share/mindie-agent", domain="vllm-ascend",
                no_public_feed=False, interactive=False, community_repository=None,
                community_project_root=None, community_account=None, community_fork=None,
                community_visibility=None, community_branch=None)
            with redirect_stdout(sys.stderr):
                install(args, argparse.ArgumentParser(), validation=receipt["validation"])
        return _installed_profile(config, receipt)


def session_start(config):
    from service_client import connect
    from bootstrap import preparing
    adapter = read(config)
    # SessionStart may start this exact installation, never activate another
    # release or alter an existing profile's runtime identity.
    with preparing("service-start"), update_lock(config):
        connect(adapter["engine_config"])
    from feed_sync import sync
    with preparing("feed-refresh"):
        feed = sync(adapter["engine_config"])
    from diagnostic_maintenance import maintain
    with preparing("diagnostic-maintenance"):
        diagnostics = maintain(config)
    print(json.dumps(dict(status="ready", feed=feed, diagnostics=diagnostics)), file=sys.stderr)
    if (feed.get("status") != "ok"
            or diagnostics.get("status") not in {"ok", "complete", "unchanged"}):
        raise RuntimeError("MindIE session preparation is incomplete; inspect local status")


def dispatch(args, config):
    remaining = args.arguments
    if args.role == 'session-start':
        session_start(config)
        return
    if args.role == 'sync':
        from feed_sync import sync
        value = sync(read(config)['engine_config'])
        print(json.dumps(value))
        return 0 if value['status'] == 'ok' else 1
    if args.role == 'configure':
        target, arguments = 'setup.py', ['configure', '--config', str(config), *remaining]
    elif args.role == 'bridge':
        target, arguments = 'bridge.py', remaining
    elif args.role == 'status':
        target, arguments = 'bridge.py', ['status', *remaining]
    else:
        target, arguments = {'stop': ('bridge.py', ['stop']),
                             'knowledge-mcp': ('bridge.py', ['mcp']),
                             'remote-mcp': ('remote_bridge.py', [])}[args.role]
    script = SCRIPTS / target
    sys.argv = [str(script), *arguments]
    runpy.run_path(str(script), run_name='__main__')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('role', choices=['session-start', 'stop', 'knowledge-mcp', 'remote-mcp', 'sync', 'configure', 'status', 'bridge'])
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    from bootstrap import preparing
    from session_gate import runtime_env
    with preparing("profile-binding"):
        config = profile(
            args.release, args.source if args.role == 'session-start' else None,
            prepare=args.role != 'stop')
        with update_lock(config):
            # Bind before any helper side effect. MCP keeps this identity in
            # memory and reacquires the same lock for each request.
            _installed_profile(config, read(args.release / "runtime.json"))
            bind_explicit_config(config, binding=runtime_binding(read(config)))
            os.environ.update(runtime_env(config))
            mcp = args.role in {'knowledge-mcp', 'remote-mcp'} or (
                args.role == 'bridge' and args.arguments[:1] == ['mcp'])
            if not mcp:
                return dispatch(args, config)
    return dispatch(args, config)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        if isinstance(exc, ProfileUnavailable):
            print(str(exc), file=sys.stderr)
        if not getattr(exc, "_mindie_prepare_reported", False):
            from diagnostic_support import failure
            failure('entry.prepare', 'release-entry', 'configuration',
                    exception=exc, reportable=False)
            print(f'MindIE release entry failed: {type(exc).__name__}; inspect local diagnostics.', file=sys.stderr)
        raise SystemExit(1)
