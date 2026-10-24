"""Execute this installed release; no native mutation or current-generation selector.

[Role]
Fixed release entrypoint (`releases/<version>/entry.py`) that validates `runtime.json` and `codex.json`, initializes the default profile on first `session-start`/MCP launch, and dispatches `session-start`, `stop`, `knowledge-mcp`, `remote-mcp`, `tools-mcp`, `sync`, `configure`, `status`, or `bridge`.

[Invariants]
- `stop` never prepares an unconfigured installation and snapshots the active `adapter` binding before reading stdin.
- Verifies `receipt["python"] == sys.executable` and `product_validation` consistency before dispatching.

[Timeouts & Locks]
- Acquires `update_lock(config)` (`blocking=True` except on `stop`) around profile validation and initialization.
- Session maintenance has a 300-second execution budget for reporter, feed,
  and knowledge readiness, followed by bounded owned-process cleanup.
- Release dispatch to helper scripts is bounded at 120.0s.

[Side-Effect Policy]
- First-use `profile(..., prepare=True)` runs headless `setup.install` under `exclusive=True` `update_lock`; subsequent calls validate and dispatch without mutating config.
"""
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
from session_gate import bind_explicit_config, runtime_scripts, runtime_env
from setup import install
from update_lock import update_lock
SCRIPTS = Path(__file__).resolve().parent


def read(path):
    return product_contract.read_json(path)[0]


class ProfileUnavailable(ValueError):
    """Static caller guidance for an unsupported or replaced installation."""


def _installed_profile(config, receipt, *, adapter=None):
    adapter = read(config) if adapter is None else adapter
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
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def session_start(config, *, session_id=None):
    from bounded_process import run
    adapter = read(config)
    environment = runtime_env(config)
    if session_id is not None:
        environment["CODEX_THREAD_ID"] = session_id
    result = run([adapter["python"], "-I",
                  str(Path(runtime_scripts(adapter)) / "session_maintenance.py"), str(config)],
                 "", timeout=300.0, env=environment, allow_service=True)
    print(result.checked_stdout(), file=sys.stderr)


def dispatch(args, config, *, stop_adapter=None):
    remaining = args.arguments
    if args.role == 'user-prompt-submit':
        from manual_entry import action, authorize
        result = authorize(read(config), args.prompt_event)
        if action(args.prompt_event) != "deactivate":
            try:
                session_start(config, session_id=args.prompt_event["session_id"])
            except Exception as exc:
                # The grant is committed. Readiness failure must remain
                # visible without discarding that known activation result.
                result["systemMessage"] = (
                    "MindIE session entry succeeded, but knowledge preparation failed "
                    f"({type(exc).__name__}); inspect local status before recovery.")
        print(json.dumps(result))
        return
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
                             'tools-mcp': ('mcp_gate.py', ['components']),
                             'remote-mcp': ('remote_bridge.py', [])}[args.role]
    if args.role in {"knowledge-mcp", "remote-mcp", "tools-mcp"} or (
            args.role == "bridge" and remaining[:1] == ["mcp"]):
        # This stable facade owns the native protocol. Each tools/call
        # dispatches the complete selected helper under the operation lock.
        script = SCRIPTS / target
        sys.argv = [str(script), *arguments]
        runpy.run_path(str(script), run_name='__main__')
    else:
        from bounded_process import run
        adapter = stop_adapter if stop_adapter is not None else read(config)
        script = Path(runtime_scripts(adapter)) / target
        data = ""
        if args.role == "stop":
            from bridge import _read_hook_stdin
            data = _read_hook_stdin().decode("utf-8")
            data = json.dumps(dict(adapter=adapter, hook_input=data), ensure_ascii=False)
            arguments = ["stop", "--snapshot"]
        result = run([adapter["python"], "-I", str(script), *arguments],
                     data, timeout=120.0, env=runtime_env(config, adapter=adapter), allow_service=True,
                     allowed_returncodes=(0, 1, 2))
        print(result.checked_stdout(), end="")
        return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('role', choices=['session-start', 'user-prompt-submit', 'stop', 'knowledge-mcp', 'remote-mcp', 'tools-mcp', 'sync', 'configure', 'status', 'bridge'])
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.role == 'user-prompt-submit':
        from manual_entry import read_event, requested
        args.prompt_event = read_event(sys.stdin.buffer)
        if not requested(args.prompt_event):
            print("{}")
            return
    from bootstrap import preparing
    from session_gate import runtime_env
    with preparing("profile-binding"):
        if args.role == "stop":
            # Atomic canonical publication gives this invocation one complete
            # runtime identity; its immutable engine can enqueue after retirement.
            receipt = read(args.release / "runtime.json")
            if receipt["python"] != sys.executable:
                raise ValueError("release interpreter differs from its prepared runtime")
            config = Path(os.environ.get("MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json")).expanduser().absolute()
            adapter = read(config)
            _installed_profile(config, receipt, adapter=adapter)
            bind_explicit_config(config)
            return dispatch(args, config, stop_adapter=adapter)
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
