"""Prepare this plugin's uv-locked, noneditable runtime; never select an update.

[Role]
Bootstraps the plugin's isolated `uv` virtual environment under `PLUGIN_DATA/runtimes/<key>`, validates the installed candidate (`candidate_validate.py`), and publishes `releases/<version>/runtime.json` and `entry.py`.

[Invariants]
- Never installs editable packages or modifies an already published `runtime.json` if source/ABI differs.
- Records preparation failures via stdlib `diagnostic_support.failure` before runtime packages exist.
- Typed profile failures retain their configuration category instead of entering
  the updater's internal-failure classifier.

[Timeouts & Locks]
- Acquires `exclusive_lock(release / "prepare.lock")` across bounded `uv sync` (`540.0s`), `readback` (`60.0s`), `probe`, and `runtime.json` publication.
- Pre/post-build storage reclamation is dispatched to a healthy managed interpreter within a 12s/20s outer bound; it never holds the prepare lock.
- A cross-volume default uv cache is redirected to a same-volume `<data>/.uv-cache` before sync.

[Side-Effect Policy]
- Writes under `PLUGIN_DATA` and an owned temporary build workspace; atomic `os.replace()` publishes `runtime.json`.
- A successful sync removes its own `prepare-*.log`; failures retain only the newest bounded constructor logs.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import sysconfig
import tempfile
import time

from product_contract import (
    failure_receipt, identity, native_version, package_files, probe, read_json,
    source_identity, source_root, validate_receipt,
)
from bootstrap_lock import exclusive_lock
from bounded_process import run
import storage_gc
from windows_process import exec_or_wait

LAST_CLEANUP = None
PREPARE_LOG_RETENTION = 3
PREPARE_LOG_SECONDS = 24 * 3600


def _record_cleanup(value):
    """Keep the latest bounded cleanup receipt for local diagnostics/tests."""
    global LAST_CLEANUP
    LAST_CLEANUP = value
    return value


def _healthy_python(value):
    if not isinstance(value, (str, os.PathLike)):
        return None
    try:
        path = Path(value)
        return path if path.is_file() and os.access(path, os.X_OK) else None
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None


def _prebuild_cleanup(python, data):
    """Reclaim disposable history before a large uv sync on a full volume."""
    if python is None:
        return None
    return _record_cleanup(storage_gc.dispatch_cleanup(
        python, data=data, keep_paths=[python], budget_seconds=1.5, timeout=12.0))


def _postbuild_cleanup(python, data, keep=()):
    """A verified runtime is never hidden by a later cleanup warning."""
    keep_paths = [python, *(item for item in keep if item)]
    try:
        value = storage_gc.dispatch_cleanup(
            python, data=data, keep_paths=keep_paths,
            budget_seconds=2.0, timeout=20.0)
    except TimeoutError:
        value = {"status": "deferred", "reason": "budget_exhausted"}
    return _record_cleanup(value)


def _unlink_prepare_log(path):
    try:
        Path(path).unlink(missing_ok=True)
    except TimeoutError:
        return "TimeoutError"
    except OSError as exc:
        return type(exc).__name__
    return None


def _prune_prepare_logs(data, release, keep=None):
    """Retain only the newest constructor logs; cleanup never masks a build."""
    candidates = []
    try:
        candidates.extend(Path(release).glob("prepare-*.log"))
        releases = Path(data) / "releases"
        for directory in list(releases.iterdir())[:64]:
            if directory.is_dir():
                candidates.extend(directory.glob("prepare-*.log"))
    except TimeoutError:
        raise
    except OSError:
        return
    rows = []
    seen = set()
    for path in candidates:
        name = str(path)
        if name in seen:
            continue
        try:
            modified = path.stat().st_mtime
        except FileNotFoundError:
            continue
        except TimeoutError:
            raise
        except OSError:
            continue
        seen.add(name)
        rows.append((modified, name, path))
    rows.sort(key=lambda item: item[0], reverse=True)
    current = str(keep) if keep is not None else None
    retained = {current} if current is not None else set()
    now = time.time()
    recent = 0
    for mtime, name, _path in rows:
        if name == current:
            continue
        if recent >= PREPARE_LOG_RETENTION or now - mtime >= PREPARE_LOG_SECONDS:
            break
        retained.add(name)
        recent += 1
    for _mtime, name, path in rows:
        if name not in retained:
            _unlink_prepare_log(path)


def ensure_runtime(source, data):
    """Keep an independently updated runtime when native reloads this plugin."""
    from session_gate import config_path
    config = config_path()
    current = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
    active = current.get("component_release")
    if active:
        receipt = read_json(Path(active) / "runtime.json")[0]
        if receipt["validation"]["source_sha256"] == source_identity(source):
            python = Path(receipt["python"])
            scripts = readback(python, source)
            if os.name == "nt":
                _probe_component_runtime(python, scripts, receipt["validation"])
            return python, scripts, Path(active)
    floor = _healthy_python(current.get("python"))
    scripts_source = Path(source) / "scripts"
    if floor is not None and (active or current.get("component_commands")):
        # Codex starts remote-mcp and tools-mcp concurrently on thread/start,
        # while component_runtime.build uses a non-blocking generation lock.
        # Serialize inheritance per profile (865s >= 830s + 20s + 15s grace).
        inherit_lock = config.with_name(config.name + ".inherit.lock")
        with exclusive_lock(inherit_lock, timeout=865):
            current = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
            active = current.get("component_release")
            if active:
                receipt = read_json(Path(active) / "runtime.json")[0]
                if receipt["validation"]["source_sha256"] == source_identity(source):
                    python = Path(receipt["python"])
                    scripts = readback(python, source)
                    if os.name == "nt":
                        _probe_component_runtime(python, scripts, receipt["validation"])
                    return python, scripts, Path(active)
            floor = _healthy_python(current.get("python")) or floor
            with preparing("component-inheritance"):
                result = run([str(floor), "-I", "-B", str(scripts_source / "component_update.py"),
                              str(source), str(floor), str(data), str(floor),
                              "--config", str(config)],
                             "", timeout=830).checked_stdout()
                effective = json.loads(result)
            if effective:
                python = Path(effective["python"])
                if not effective.get("reused"):
                    _postbuild_cleanup(python, data, keep=[floor])
                return python, Path(effective["scripts"]), Path(effective["release"])
    python, scripts, release = _baseline_runtime(
        source, data, cleanup_python=floor)
    return python, scripts, release


@contextmanager
def preparing(stage):
    """The existing stdlib diagnostic path works before runtime dependencies exist."""
    try:
        yield
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and (
            exc.code in (None, 0) or getattr(exc, "_mindie_forwarded_child", False)
        ):
            raise
        if getattr(exc, "_mindie_prepare_reported", False):
            raise
        from diagnostic_support import failure
        category = getattr(exc, "_mindie_prepare_category", "update")
        exit_code = (
            exc.code
            if isinstance(exc, SystemExit) and type(exc.code) is int
            else getattr(getattr(exc, "process_result", None), "returncode", None)
        )
        diagnostic = failure("runtime.prepare", stage, category, exception=exc,
                             exit_code=exit_code)
        exc.mindie_diagnostic = diagnostic
        detail = f"MindIE runtime preparation failed at {stage}: {type(exc).__name__}"
        from product_contract import CandidateValidationError
        if isinstance(exc, CandidateValidationError):
            detail += f" ({exc.stage}:{exc.code})"
        result = getattr(exc, "process_result", None)
        if result is not None:
            detail += f" (execution={result.execution}, exit={result.returncode}, cleanup_failed={bool(result.cleanup)})"
        print(detail + "; " + json.dumps(diagnostic, sort_keys=True), file=sys.stderr)
        exc._mindie_prepare_stage = stage
        exc._mindie_prepare_reported = True
        raise


def runtime_key(source):
    identity = source_identity(source)
    abi = f"{sys.implementation.cache_tag}:{sysconfig.get_platform()}"
    return hashlib.sha256(f"{identity}:{abi}".encode()).hexdigest()


def native_path(path):
    """Bootstrap runs before the shared filesystem helpers are installed."""
    path = os.path.abspath(path)
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        path = "\\\\?\\" + ("UNC\\" + path[2:] if path.startswith("\\\\") else path)
    return Path(path)


def readback(python, source):
    code = """
import importlib.metadata as m, importlib.util, json, pathlib, sys
d=m.distribution('mindie-agent-codex')
if json.loads(d.read_text('direct_url.json') or '{}').get('dir_info',{}).get('editable'):
    raise ValueError('editable adapter runtime is unsupported')
root=pathlib.Path(importlib.util.find_spec('mindie_codex').origin).parent
print(json.dumps({'root':str(root),'prefix':sys.prefix,'version':d.version}))
"""
    value = json.loads(run([str(python), "-I", "-c", code], "", timeout=60.0).checked_stdout())
    if value["version"] != native_version(source):
        raise ValueError("installed distribution differs from native plugin version")
    installed = Path(value["root"])
    if Path(value["prefix"]).absolute() != python.parent.parent.absolute():
        raise ValueError("runtime interpreter identity differs")
    for path in package_files(source):
        target = installed / path.relative_to(source)
        if not target.is_file() or target.read_bytes() != path.read_bytes():
            raise ValueError("installed adapter differs from native plugin")
    return installed / "scripts"


def _probe_component_runtime(python, scripts, validation):
    """Revalidate a cached component runtime without importing it in this host.

    The stdlib bootstrap process must never import `mindie_knowledge`: only the
    installed candidate runtime can resolve its component identity. Reuse the
    stdlib-only contract helpers here and delegate that check to the candidate.
    """
    if not isinstance(validation, dict) or validation.get("status") != "validated":
        raise ValueError("candidate validation receipt does not match the requested source")
    source = source_root(scripts)
    baseline = identity(source, validation.get("candidate_revision"))
    if any(validation.get(key) != value
           for key, value in baseline.items() if key != "runtime"):
        raise ValueError("candidate validation receipt does not match the requested source")
    if "component_updates" not in validation and validation != dict(baseline, status="validated"):
        raise ValueError("candidate validation receipt does not match the requested source")
    expected = {key: value for key, value in validation.items() if key != "status"}
    argv = [str(python), "-I", "-B", str(Path(scripts) / "candidate_validate.py"),
            "--source", str(source),
            "--identity", json.dumps(expected, sort_keys=True),
            "--verified-receipt", json.dumps(validation, sort_keys=True),
            "--require-windows-service"]
    output = run(argv, "", timeout=180.0, max_output=65536,
                 on_failure=lambda out, code: failure_receipt(out, expected, code)).checked_stdout()
    receipt = validate_receipt(output, expected)
    if identity(source, validation.get("candidate_revision")) != baseline:
        raise ValueError("candidate source changed during validation")
    return receipt


def _baseline_runtime(source, data, cleanup_python=None):
    deadline = time.monotonic() + 900
    def budget(maximum, reserve):
        remaining = deadline - time.monotonic() - reserve
        if remaining <= 0:
            raise TimeoutError('runtime preparation budget exhausted')
        return min(maximum, remaining)
    with preparing("source-identity"):
        source, data = Path(source).resolve(), Path(data).absolute()
        version = native_version(source)
        key = runtime_key(source)
        release = data / "releases" / version
        release.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Windows dependency files must fit beneath Codex's default plugin-data path.
    # The receipt keeps the full source/ABI digest; only the directory is shorter.
    runtime = data / "runtimes" / (key[:24] if os.name == "nt" else key)
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = runtime / executable
    receipt = release / "runtime.json"
    if not receipt.exists():
        _prebuild_cleanup(cleanup_python, data)
    with exclusive_lock(release / "prepare.lock",
                        timeout=budget(645, 255) if os.name == "nt" else budget(825, 75)):
        if receipt.exists():
            existing = json.loads(receipt.read_text(encoding="utf-8"))
            bound_python = Path(existing["python"])
            if existing.get("key") != key or bound_python not in (
                python, data / "runtimes" / key / executable,
                native_path(python), native_path(data / "runtimes" / key / executable),
            ):
                raise ValueError("immutable plugin version has different source or Python ABI")
            # An existing release remains bound to its published runtime path.
            python = bound_python
            if str(python).startswith("\\\\?\\"):
                release = native_path(release)
            with preparing("completed-runtime"):
                budget(60, 195 if os.name == "nt" else 15)
                scripts = readback(python, source)
                if os.name == "nt":
                    # A cached generic receipt does not prove this machine's
                    # generated pythonw.exe; recheck the launcher locally.
                    probe(python, scripts,
                          lambda argv, **kw: run(
                              argv, "",
                              **dict(kw, timeout=budget(kw.get("timeout", 180.0), 15))
                          ).checked_stdout(),
                          verified_receipt=existing["validation"],
                          require_windows_service=True)
                else:
                    validate_receipt(json.dumps(existing["validation"]), identity(scripts.parent))
                publish_entry(python, scripts, release)
            return python, scripts, release
        # The interpreter spelling also determines sys.prefix. Retain the
        # extended namespace so importlib can read long dependency resources.
        python, release = native_path(python), native_path(release)
        receipt = release / "runtime.json"
        from native_context import which_host_binary
        uv = which_host_binary("uv", which_fn=shutil.which)
        if uv is None:
            raise FileNotFoundError("uv is required to prepare MindIE's locked runtime")
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env.update(UV_PROJECT_ENVIRONMENT=str(runtime), UV_NO_PROGRESS="1",
                   GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        storage_gc.configure_bootstrap_uv_cache(data, runtime, env)
        if "UV_SYSTEM_CERTS" not in env and "UV_NATIVE_TLS" not in env:
            env["UV_SYSTEM_CERTS"] = "true"
            env["UV_NATIVE_TLS"] = "true"
        elif "UV_SYSTEM_CERTS" not in env:
            env.setdefault("UV_NATIVE_TLS", "true")
        if "GIT_CONFIG_COUNT" not in env:
            git_pairs = (
                ("core.longpaths", "true"),
                ("core.autocrlf", "false"),
                ("credential.https://github.com.provider", "github"),
            )
            env["GIT_CONFIG_COUNT"] = str(len(git_pairs))
            for idx, (k, v) in enumerate(git_pairs):
                env[f"GIT_CONFIG_KEY_{idx}"] = k
                env[f"GIT_CONFIG_VALUE_{idx}"] = v
        # uv locks its target environment. An interrupted, unpublished build
        # can be synced again; a completed runtime above is never repaired.
        with preparing("locked-sync"):
            # Failed installer output stays private and separate from native
            # Hook output. It is diagnostic evidence, never readiness authority.
            descriptor, log = tempfile.mkstemp(prefix="prepare-", suffix=".log", dir=release)
            os.close(descriptor)
            # Keep the redirector alive until the real installer exits. Windows
            # CRT exec starts a new PID; treating that redirector exit as uv's
            # completion would make the owned runner terminate uv prematurely.
            redirect = ("import os,sys,subprocess;"
                        "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_APPEND);"
                        "rc=subprocess.run(sys.argv[2:],stdin=subprocess.DEVNULL,stdout=fd,stderr=fd,"
                        "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).returncode;"
                        "os.close(fd);sys.exit(rc)")
            # Build backends may write build/ and *.egg-info into the project.
            # Native hook trust hashes the installed plugin payload, so even a
            # noneditable build must operate outside that published payload.
            # Build backends form additional relative wheel paths. A short
            # owned workspace also keeps those paths within Windows limits.
            with tempfile.TemporaryDirectory(prefix="mindie-build-") as directory:
                project = Path(directory)
                for path in package_files(source):
                    target = project / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                synced = False
                try:
                    run([sys.executable, "-I", "-B", "-c", redirect, log, uv, "sync",
                         "--project", str(project), "--locked", "--no-editable", "--no-dev",
                         "--python", sys.executable], "", timeout=budget(540, 255), env=env).checked_stdout()
                    synced = True
                finally:
                    if not synced:
                        try:
                            _prune_prepare_logs(data, release, keep=Path(log))
                        except TimeoutError:
                            pass
        completed = False
        try:
            with preparing("installed-readback"):
                budget(60, 195)
                scripts = readback(python, source)
            with preparing("candidate-validation"):
                validation = probe(python, scripts,
                                   lambda argv, **kw: run(
                                       argv, "",
                                       **dict(kw, timeout=budget(kw.get("timeout", 180.0), 15))
                                   ).checked_stdout(),
                                   require_windows_service=(os.name == "nt"))
            with preparing("release-publication"):
                temporary = release / "runtime.json.tmp"
                temporary.write_text(json.dumps({"key": key, "python": str(python),
                                                 "validation": validation}))
                os.replace(temporary, receipt)
                publish_entry(python, scripts, release)
            completed = True
        finally:
            if completed:
                _unlink_prepare_log(log)
            else:
                try:
                    _prune_prepare_logs(data, release, keep=Path(log))
                except TimeoutError:
                    pass
        _postbuild_cleanup(python, data,
                           keep=[cleanup_python] if cleanup_python else [])
        return python, scripts, release


def publish_entry(python, scripts, release):
    """One fixed release binding. Its source never reads a mutable current pointer."""
    text = (
        "import os,sys\n"
        f"python={str(python)!r}\n"
        f"entry={str(scripts / 'entry.py')!r}\n"
        "env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}\n"
        f"release={str(release)!r}\n"
        f"sys.path.insert(0,{str(scripts)!r})\n"
        "from windows_process import exec_or_wait\n"
        "exec_or_wait([python,'-I',entry,'--release',release,*sys.argv[1:]],env)\n"
    )
    target = release / "entry.py"
    if target.exists():
        if target.read_text(encoding="utf-8") != text:
            raise ValueError("immutable release entry differs")
        _publish_hook_launcher(scripts, release)
        return target
    fd, raw_tmp = tempfile.mkstemp(prefix="entry.py.", suffix=".tmp", dir=release)
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_text(encoding="utf-8") != text:
                raise ValueError("immutable release entry differs")
    finally:
        temporary.unlink(missing_ok=True)
    _publish_hook_launcher(scripts, release)
    return target


def _publish_hook_launcher(scripts, release):
    """Keep durable copies so hooks/MCP can locate the selector after cache eviction."""
    source = Path(scripts) / "hook_launcher.py"
    if not source.is_file():
        return
    text = source.read_text(encoding="utf-8")
    target = Path(release) / "hook_launcher.py"
    if target.exists():
        if target.read_text(encoding="utf-8") != text:
            raise ValueError("immutable release launcher differs")
    else:
        fd, raw_tmp = tempfile.mkstemp(prefix="hook_launcher.py.", suffix=".tmp", dir=release)
        temporary = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_text(encoding="utf-8") != text:
                    raise ValueError("immutable release launcher differs")
        finally:
            temporary.unlink(missing_ok=True)
    rel_path = Path(release)
    if rel_path.parent.name == "releases":
        data_root = rel_path.parent.parent
    elif rel_path.name == "release" and len(rel_path.parents) >= 3 and rel_path.parents[1].name == "component-runtimes":
        data_root = rel_path.parents[2]
    else:
        return
    durable = data_root / "hook_launcher.py"
    staging = rel_path / "hook_launcher.durable.tmp"
    staging.write_text(text, encoding="utf-8", newline="\n")
    os.replace(staging, durable)


_PROMPT_GUARDED = False
_PROMPT_EVENT = None


def _restore_stdin_bytes(raw):
    if len(raw) <= 4096:
        reader, writer = os.pipe()
        try:
            os.write(writer, raw)
        finally:
            os.close(writer)
        try:
            os.dup2(reader, 0)
        finally:
            os.close(reader)
        return
    with tempfile.TemporaryFile() as stream:
        stream.write(raw)
        stream.flush()
        stream.seek(0)
        os.dup2(stream.fileno(), 0)


def _guard_passive_prompt():
    """Return False and emit {} for passive prompts before any runtime/diagnostic write."""
    global _PROMPT_GUARDED, _PROMPT_EVENT
    if _PROMPT_GUARDED:
        return True
    _PROMPT_GUARDED = True
    if "user-prompt-submit" not in sys.argv[2:] or "migrate" in sys.argv[2:]:
        return True
    raw = sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
    if len(raw) <= 32 * 1024 * 1024:
        try:
            event = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, TypeError):
            event = None
        if isinstance(event, dict):
            from manual_entry import requested
            if not requested(event):
                print("{}")
                return False
            _PROMPT_EVENT = event
    _restore_stdin_bytes(raw)
    return True


def main():
    if not _guard_passive_prompt():
        return
    source = Path(__file__).resolve().parents[1]
    data = Path(sys.argv[1])
    python, scripts, release = ensure_runtime(source, data)
    entry = release / "entry.py"
    exec_or_wait([str(python), str(entry), *sys.argv[2:]], dict(os.environ))


if __name__ == "__main__":
    try:
        if not _guard_passive_prompt():
            raise SystemExit(0)
        with preparing("bootstrap"):
            main()
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and (
            exc.code in (None, 0) or getattr(exc, "_mindie_forwarded_child", False)
        ):
            raise
        if _PROMPT_EVENT is not None:
            from manual_entry import prompt_failure_response, record_entry_status
            config = Path(os.environ.get(
                "MINDIE_AGENT_CONFIG",
                Path.home() / ".config/mindie-agent/codex.json",
            )).expanduser().absolute()
            stage = getattr(exc, "_mindie_prepare_stage", None) or "bootstrap"
            try:
                record_entry_status(
                    config,
                    stage=stage,
                    status="failed",
                    session_id=_PROMPT_EVENT.get("session_id"),
                    error_type=type(exc).__name__,
                )
            except Exception:
                pass
            print(json.dumps(prompt_failure_response(stage, type(exc).__name__)))
            raise SystemExit(0)
        if isinstance(exc, SystemExit):
            raise
        raise SystemExit(1)
