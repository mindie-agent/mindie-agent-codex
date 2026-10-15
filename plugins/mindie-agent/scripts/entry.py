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
from session_gate import bind_explicit_config, runtime_scripts, runtime_env
from setup import install
from update_lock import update_lock
SCRIPTS = Path(__file__).resolve().parent


def read(path):
    return product_contract.read_json(path)[0]


class ProfileUnavailable(ValueError):
    """Static caller guidance for an unsupported or replaced installation."""


def _installed_profile(config, receipt):
    adapter = read(config)
    python = adapter.get("python")
    scripts = Path(runtime_scripts(adapter))
    if not isinstance(python, str) or not os.path.isabs(python):
        raise ProfileUnavailable("installed profile lacks an absolute runtime interpreter")
    engine = Path(adapter["engine_config"])
    value = read(engine)
    if ("client_contract" in value
            or value.get("transcript_adapter") != str(scripts / "codex_transcript.py")
            or value.get("product_validation") != adapter.get("product_validation")):
        raise ProfileUnavailable("unsupported installation profile; configuration, data and consent are preserved")
    return config


def profile(release, source=None, *, prepare=True):
    receipt = read(release / "runtime.json")
    if receipt["python"] != sys.executable:
        raise ValueError("release interpreter differs from its prepared runtime")
    config = Path(os.environ.get("MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json")).expanduser().absolute()
    if os.path.lexists(config):
        with update_lock(config, blocking=prepare):
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
    from bounded_process import run
    adapter = read(config)
    result = run([adapter["python"], "-I",
                  str(Path(runtime_scripts(adapter)) / "session_maintenance.py"), str(config)],
                 "", env=runtime_env(config), allow_service=True)
    print(result.checked_stdout(), file=sys.stderr)


def dispatch(args, config):
    remaining = args.arguments
    if args.role == 'session-start':
        session_start(config)
        return
    if args.role == 'sync':
        target, arguments = 'feed_sync.py', [str(config)]
    elif args.role == 'configure':
        target, arguments = 'setup.py', ['configure', '--config', str(config), *remaining]
    elif args.role == 'bridge':
        target, arguments = 'bridge.py', remaining
    elif args.role == 'status':
        target, arguments = 'bridge.py', ['status', *remaining]
    else:
        target, arguments = {'stop': ('bridge.py', ['stop']),
                             'knowledge-mcp': ('bridge.py', ['mcp']),
                             'remote-mcp': ('remote_bridge.py', [])}[args.role]
    if args.role in {"knowledge-mcp", "remote-mcp"} or (
            args.role == "bridge" and remaining[:1] == ["mcp"]):
        # This stable facade owns the native protocol. Each tools/call
        # dispatches the complete selected helper under the operation lock.
        script = SCRIPTS / target
        sys.argv = [str(script), *arguments]
        runpy.run_path(str(script), run_name='__main__')
    else:
        from bounded_process import run
        adapter = read(config)
        script = Path(runtime_scripts(adapter)) / target
        data = sys.stdin.read() if args.role == "stop" else ""
        result = run([adapter["python"], "-I", str(script), *arguments],
                     data, env=runtime_env(config), allow_service=True,
                     allowed_returncodes=(0, 1, 2))
        print(result.checked_stdout(), end="")
        return result.returncode


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
        with update_lock(config, blocking=args.role != 'stop'):
            # Bind before any helper side effect. MCP keeps this identity in
            # memory and reacquires the same lock for each request.
            _installed_profile(config, read(args.release / "runtime.json"))
            bind_explicit_config(config)
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
