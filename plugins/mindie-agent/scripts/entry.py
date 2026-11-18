"""Execute this installed release; no native mutation or current-generation selector.

[Role]
Fixed release entrypoint (`releases/<version>/entry.py`) that validates `runtime.json` and `codex.json`, initializes the default profile on first `session-start`/MCP launch, and dispatches `session-start`, `stop`, `knowledge-mcp`, `remote-mcp`, `tools-mcp`, `sync`, `configure`, `status`, or `bridge`. The hidden `migrate` role activates a newer installed seed before dispatching an MCP role; hooks and Stop never migrate.

[Invariants]
- `stop` never prepares an unconfigured installation and snapshots the active `adapter` binding before reading stdin.
- Verifies `receipt["python"] == sys.executable` and profile consistency. A
  cutover after launcher selection transfers startup to the committed release
  before any operation dispatch; old and new runtime packages never mix.

[Timeouts & Locks]
- Acquires `update_lock(config)` (`blocking=True` except on `stop`) around profile validation and initialization.
- Every long-lived MCP role, including `tools-mcp`, releases that shared lock before serve dispatch; `Gate.call` acquires it again only for an actual tool call.
- Every long-lived MCP role holds `acquire_runtime_lease` on its owning managed
  generation for the entire serve loop; non-blocking lease failure falls back
  to live-process protection instead of refusing the session.
- Session maintenance has a 300-second execution budget for reporter, feed,
  and knowledge readiness, followed by bounded owned-process cleanup.
- Release dispatch to helper scripts is bounded at 120.0s.
- A consumed explicit prompt is forwarded within 420s (300s maintenance plus
  admission/entry and cleanup); unread MCP streams transfer without buffering.

[Side-Effect Policy]
- First-use `profile(..., prepare=True)` runs headless `setup.install` under `exclusive=True` `update_lock`; subsequent calls validate and dispatch without mutating config.
"""
from __future__ import annotations
from contextlib import contextmanager, redirect_stdout
import json
import os
from pathlib import Path
import re
import sys

# All native entries use -I and this exact installed package.
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import product_contract
import session_gate as session_gate_mod
from session_gate import bind_explicit_config, runtime_scripts, runtime_env


def __getattr__(name):
    if name == "install":
        from setup import install as _install
        return _install
    if name == "update_lock":
        from update_lock import update_lock as _update_lock
        return _update_lock
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@contextmanager
def _lazy_preparing(stage):
    try:
        yield
    except (Exception, SystemExit):
        from bootstrap import preparing
        with preparing(stage):
            raise


def read(path):
    return product_contract.read_json(path)[0]


class ProfileUnavailable(ValueError):
    """Static caller guidance for an unsupported or replaced installation."""
    _mindie_prepare_category = "configuration"


def _installed_profile(config, receipt, *, adapter=None):
    adapter = read(config) if adapter is None else adapter
    if not isinstance(adapter, dict) or not isinstance(receipt, dict):
        raise ProfileUnavailable("installed profile metadata must be JSON objects")
    python = adapter.get("python")
    try:
        scripts = Path(runtime_scripts(adapter))
    except ValueError as exc:
        raise ProfileUnavailable("installed profile lacks an absolute runtime_scripts path") from exc
    if not isinstance(python, str) or not os.path.isabs(python):
        raise ProfileUnavailable("installed profile lacks an absolute runtime interpreter")
    engine_raw = adapter.get("engine_config")
    if not isinstance(engine_raw, str) or not os.path.isabs(engine_raw):
        raise ProfileUnavailable("installed profile lacks an absolute engine_config path")
    engine = Path(engine_raw)
    value = read(engine)
    if (not isinstance(value, dict)
            or "client_contract" in value
            or not product_contract.same_interpreter(
                value.get("transcript_adapter"),
                str(scripts / "codex_transcript.py"))
            or value.get("product_validation") != adapter.get("product_validation")):
        raise ProfileUnavailable("unsupported installation profile; configuration, data and consent are preserved")
    return config


def profile(release, source=None, *, prepare=True):
    import argparse
    mod = sys.modules[__name__]
    receipt = read(release / "runtime.json")
    if not isinstance(receipt, dict) or not product_contract.same_interpreter(receipt.get("python"), sys.executable):
        raise ValueError("release interpreter differs from its prepared runtime")
    config = Path(os.environ.get("MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json")).expanduser().absolute()
    if os.path.lexists(config):
        with mod.update_lock(config, blocking=prepare):
            return _installed_profile(config, receipt)
    if not prepare:
        raise RuntimeError("MindIE release profile is not prepared; start a new native session")
    if source is not None and source.is_dir():
        product_contract.validate_receipt(json.dumps(receipt["validation"]), product_contract.identity(source))
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with mod.update_lock(config, exclusive=True, blocking=True):
        if not os.path.lexists(config):
            args = argparse.Namespace(knowledge_python=sys.executable, config=config,
                root=Path.home() / ".local/share/mindie-agent", domain="vllm-ascend",
                no_public_feed=False, interactive=False, community_repository=None,
                community_project_root=None, community_account=None, community_fork=None,
                community_visibility=None, community_branch=None)
            with redirect_stdout(sys.stderr):
                mod.install(args, argparse.ArgumentParser(), validation=receipt["validation"])
        return _installed_profile(config, receipt)


def replacement_entry(release, adapter):
    """Use the canonical launcher's release identity, including first setup."""
    if not isinstance(adapter, dict):
        raise ProfileUnavailable("installed profile metadata must be a JSON object")
    validation = adapter.get("product_validation")
    if not isinstance(validation, dict):
        raise ProfileUnavailable("installed profile points at another runtime with an invalid release identity")
    version = validation.get("native_version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+!-]{0,79}", version):
        raise ProfileUnavailable("installed profile points at another runtime with an invalid release identity")
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    data = Path(os.environ.get("PLUGIN_DATA", home / "plugins/data/mindie-agent-mindie-agent"))
    comp_rel = adapter.get("component_release")
    if comp_rel is not None and (not isinstance(comp_rel, str) or not os.path.isabs(comp_rel)):
        raise ProfileUnavailable("installed profile points at another runtime without a prepared entry")
    target = Path(comp_rel) if comp_rel else data / "releases" / version
    if (product_contract.same_directory(str(target), str(release))
            or not (target / "entry.py").is_file()
            or not (target / "runtime.json").is_file()):
        raise ProfileUnavailable("installed profile points at another runtime without a prepared entry")
    receipt = read(target / "runtime.json")
    if (not isinstance(receipt, dict)
            or not product_contract.same_interpreter(receipt.get("python"), adapter.get("python"))
            or receipt.get("validation") != validation
            or validation.get("schema") != "mindie-candidate-check/1"
            or validation.get("status") != "validated"):
        raise ProfileUnavailable("installed profile differs from its prepared runtime receipt")
    return [adapter["python"], "-I", str(target / "entry.py")]


def transfer_entry(command, args, environment):
    command = [*command, args.role, *args.arguments]
    if args.role == "user-prompt-submit":
        # Ordinary prompts already returned. This explicit event was consumed
        # before profile resolution; forward it once, before granting a lease.
        # Keep Unicode in UTF-8 and omit added whitespace within the native byte budget.
        from bounded_process import run
        data = json.dumps(args.prompt_event, ensure_ascii=False, separators=(",", ":"))
        result = run(command, data, timeout=420.0,
                     env=environment, allow_service=True, allowed_returncodes=None)
        # These streams were already decoded from UTF-8 pipes. A second text
        # newline conversion on Windows would turn CRLF into CRCRLF.
        sys.stderr.buffer.write(result.stderr.encode("utf-8"))
        sys.stdout.buffer.write(result.checked_stdout().encode("utf-8"))
        return result.returncode
    from windows_process import exec_or_wait
    return exec_or_wait(command, environment)


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


_ACTIVE_ARGS = None


def _stop_fast_inactive(adapter):
    """Return True when saved sharing/consent is unconfigured or disabled (skip stdin & re-exec)."""
    if not isinstance(adapter, dict):
        return False
    comm_raw = adapter.get("community_config")
    if not comm_raw:
        return True
    if not isinstance(comm_raw, str) or not os.path.isabs(comm_raw):
        return False
    comm_path = Path(comm_raw)
    try:
        if not comm_path.exists():
            return True
        if not comm_path.is_file():
            return False
        raw = comm_path.read_bytes()
        if len(raw) > 262144:
            return False
        data = json.loads(raw.decode("utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("schema") != "mindie-community-config/1":
        return False
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return False
    roots = data.get("project_roots")
    if not isinstance(roots, list):
        return False
    if not enabled or not roots:
        return True
    consent_raw = data.get("consent_config")
    if not isinstance(consent_raw, str) or not os.path.isabs(consent_raw):
        return False
    consent_path = Path(consent_raw)
    try:
        if not consent_path.exists():
            return True
        if not consent_path.is_file():
            return False
        consent_data = json.loads(consent_path.read_bytes().decode("utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False
    if not isinstance(consent_data, dict) or consent_data.get("schema") != "mindie-consent/1":
        return False
    return consent_data.get("choice") != "contribute"


def dispatch(args, config, *, stop_adapter=None):
    remaining = args.arguments
    if args.role == 'user-prompt-submit':
        print("{}")
        return 0
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
        # A resident server holds its managed generation against any GC sweep.
        import runpy
        from storage_gc import serving_runtime_lease
        script = SCRIPTS / target
        sys.argv = [str(script), *arguments]
        with serving_runtime_lease():
            runpy.run_path(str(script), run_name='__main__')
    else:
        from bounded_process import run
        adapter = stop_adapter if stop_adapter is not None else read(config)
        script = Path(runtime_scripts(adapter)) / target
        data = ""
        if args.role == "stop":
            from bridge import _read_hook_stdin
            data = _read_hook_stdin().decode("utf-8-sig")
            data = json.dumps(dict(adapter=adapter, hook_input=data), ensure_ascii=False)
            arguments = ["stop", "--snapshot"]
        result = run([adapter["python"], "-I", str(script), *arguments],
                     data, timeout=120.0, env=runtime_env(config, adapter=adapter), allow_service=True,
                     allowed_returncodes=(0, 1, 2))
        print(result.checked_stdout(), end="")
        return result.returncode


def main():
    global _ACTIVE_ARGS
    raw = sys.argv[1:]
    if len(raw) == 3 and raw[0] == "--release" and raw[2] in {"stop", "user-prompt-submit"}:
        class _FastArgs:
            release = Path(raw[1])
            source = None
            role = raw[2]
            arguments = []
        args = _FastArgs()
        _ACTIVE_ARGS = args
        if args.role == "user-prompt-submit":
            print("{}")
            return 0
        with _lazy_preparing("profile-binding"):
            receipt = read(args.release / "runtime.json")
            if not isinstance(receipt, dict) or not product_contract.same_interpreter(receipt.get("python"), sys.executable):
                raise ValueError("release interpreter differs from its prepared runtime")
            config = Path(os.environ.get("MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json")).expanduser().absolute()
            adapter = read(config)
            _installed_profile(config, receipt, adapter=adapter)
            bind_explicit_config(config)
        if _stop_fast_inactive(adapter):
            print("{}")
            return 0
        return dispatch(args, config, stop_adapter=adapter)
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('role', choices=['session-start', 'user-prompt-submit', 'stop', 'knowledge-mcp', 'remote-mcp', 'tools-mcp', 'sync', 'configure', 'status', 'bridge', 'migrate'])
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    _ACTIVE_ARGS = args
    if args.role == 'migrate':
        if not args.arguments:
            raise ValueError("migration role requires the original role")
        role, arguments = args.arguments[0], args.arguments[1:]
        if role not in {'knowledge-mcp', 'remote-mcp', 'tools-mcp', 'bridge'}:
            raise ValueError("migration cannot dispatch this role")
        if role == 'bridge' and arguments[:1] != ['mcp']:
            raise ValueError("migration cannot dispatch this bridge command")
        from release_migration import run as migrate
        outcome = migrate(args.release, role, arguments)
        if outcome.get("status") != "ready":
            print(json.dumps(outcome, sort_keys=True), file=sys.stderr)
            return 1
        args.role, args.arguments = role, arguments
    if args.role == 'user-prompt-submit':
        print("{}")
        return 0
    from bootstrap import preparing
    mod = sys.modules[__name__]
    if args.role == "stop":
        with preparing("profile-binding"):
            # Atomic canonical publication gives this invocation one complete
            # runtime identity; its immutable engine can enqueue after retirement.
            receipt = read(args.release / "runtime.json")
            if not isinstance(receipt, dict) or not product_contract.same_interpreter(receipt.get("python"), sys.executable):
                raise ValueError("release interpreter differs from its prepared runtime")
            config = Path(os.environ.get("MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json")).expanduser().absolute()
            adapter = read(config)
            _installed_profile(config, receipt, adapter=adapter)
            bind_explicit_config(config)
        if _stop_fast_inactive(adapter):
            print("{}")
            return 0
        return dispatch(args, config, stop_adapter=adapter)
    with preparing("profile-binding"):
        config = profile(
            args.release, args.source if args.role == 'session-start' else None,
            prepare=True)
    mcp = args.role in {'knowledge-mcp', 'remote-mcp', 'tools-mcp'} or (
        args.role == 'bridge' and args.arguments[:1] == ['mcp'])
    with mod.update_lock(config, blocking=True):
        with preparing("profile-binding"):
            # Bind before any helper side effect. MCP keeps this identity in
            # memory and reacquires the same lock for each request.
            adapter = read(config)
            _installed_profile(config, read(args.release / "runtime.json"), adapter=adapter)
            if (not product_contract.same_interpreter(adapter.get("python"), sys.executable)
                    or not product_contract.same_directory(runtime_scripts(adapter), str(SCRIPTS))):
                # The native facade selected this entry before activation
                # acquired the lock. No business operation has started yet.
                command = replacement_entry(args.release, adapter)
                environment = session_gate_mod.runtime_env(config, adapter=adapter)
            else:
                command = None
                bind_explicit_config(config)
                os.environ.update(session_gate_mod.runtime_env(config))
        if command is None and not mcp:
            return mod.dispatch(args, config)
    if command is not None:
        # Release the old operation lock before entering another interpreter.
        return transfer_entry(command, args, environment)
    return mod.dispatch(args, config)


if __name__ == '__main__':
    try:
        _rc = main()
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and (
            exc.code in (None, 0) or getattr(exc, "_mindie_forwarded_child", False)
        ):
            raise
        if isinstance(exc, ProfileUnavailable):
            print(str(exc), file=sys.stderr)
        if not getattr(exc, "_mindie_prepare_reported", False):
            from diagnostic_support import failure
            failure('entry.prepare', 'release-entry', 'configuration',
                    exception=exc, reportable=False,
                    exit_code=exc.code if isinstance(exc, SystemExit) and type(exc.code) is int else None)
            print(f'MindIE release entry failed: {type(exc).__name__}; inspect local diagnostics.', file=sys.stderr)
        if _ACTIVE_ARGS is not None and _ACTIVE_ARGS.role == 'user-prompt-submit':
            print("{}")
            raise SystemExit(0)
        if isinstance(exc, SystemExit):
            raise
        raise SystemExit(1)
    raise SystemExit(_rc)
