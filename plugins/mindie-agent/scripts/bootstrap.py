"""Prepare this plugin's uv-locked, noneditable runtime; never select an update.

[Role]
Bootstraps the plugin's isolated `uv` virtual environment under `PLUGIN_DATA/runtimes/<key>`, validates the installed candidate (`candidate_validate.py`), and publishes `releases/<version>/runtime.json` and `entry.py`.

[Invariants]
- Never installs editable packages or modifies an already published `runtime.json` if source/ABI differs.
- Records preparation failures via stdlib `diagnostic_support.failure` before runtime packages exist.

[Timeouts & Locks]
- Acquires `exclusive_lock(release / "prepare.lock")` across bounded `uv sync` (`540.0s`), `readback` (`60.0s`), `probe`, and `runtime.json` publication.

[Side-Effect Policy]
- Writes only under `PLUGIN_DATA/runtimes/<key>` and `PLUGIN_DATA/releases/<version>`; atomic `os.replace()` publishes `runtime.json`.
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

from product_contract import package_files, source_identity, probe, identity, validate_receipt, native_version, read_json
from bootstrap_lock import exclusive_lock
from bounded_process import run
from windows_process import exec_or_wait


@contextmanager
def preparing(stage):
    """The existing stdlib diagnostic path works before runtime dependencies exist."""
    try:
        yield
    except Exception as exc:
        if getattr(exc, "_mindie_prepare_reported", False):
            raise
        from diagnostic_support import failure
        diagnostic = failure("runtime.prepare", stage, "prerequisite", exception=exc,
                             exit_code=getattr(exc, "returncode", None), reportable=False)
        detail = f"MindIE runtime preparation failed at {stage}: {type(exc).__name__}"
        from product_contract import CandidateValidationError
        if isinstance(exc, CandidateValidationError):
            detail += f" ({exc.stage}:{exc.code})"
        result = getattr(exc, "process_result", None)
        if result is not None:
            detail += f" (execution={result.execution}, exit={result.returncode}, cleanup_failed={bool(result.cleanup)})"
        print(detail + "; " + json.dumps(diagnostic, sort_keys=True), file=sys.stderr)
        exc._mindie_prepare_reported = True
        raise


def runtime_key(source):
    identity = source_identity(source)
    abi = f"{sys.implementation.cache_tag}:{sysconfig.get_platform()}"
    return hashlib.sha256(f"{identity}:{abi}".encode()).hexdigest()


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
            return python, readback(python, source), Path(active)
    python, scripts, release = _baseline_runtime(source, data)
    if active:
        # A newer plugin may carry older component pins. Build its adapter with
        # the installed floor before publishing any binding for the new plugin.
        result = run([str(python), "-I", str(scripts / "component_update.py"),
                      str(source), str(python), str(data), current["python"]],
                     "", timeout=830).checked_stdout()
        effective = json.loads(result)
        if effective:
            return Path(effective["python"]), Path(effective["scripts"]), Path(effective["release"])
    return python, scripts, release


def _baseline_runtime(source, data):
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
    with exclusive_lock(release / "prepare.lock", timeout=budget(825, 75)):
        if receipt.exists():
            existing = json.loads(receipt.read_text(encoding="utf-8"))
            bound_python = Path(existing["python"])
            if existing.get("key") != key or bound_python not in (
                python, data / "runtimes" / key / executable
            ):
                raise ValueError("immutable plugin version has different source or Python ABI")
            # An existing release remains bound to its published runtime path.
            python = bound_python
            with preparing("completed-runtime"):
                scripts = readback(python, source)
                validate_receipt(json.dumps(existing["validation"]), identity(scripts.parent))
                publish_entry(python, scripts, release)
            return python, scripts, release
        uv = shutil.which("uv")
        if uv is None:
            raise FileNotFoundError("uv is required to prepare MindIE's locked runtime")
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env.update(UV_PROJECT_ENVIRONMENT=str(runtime), UV_NO_PROGRESS="1",
                   GIT_TERMINAL_PROMPT="0")
        if "UV_SYSTEM_CERTS" not in env:
            env.setdefault("UV_NATIVE_TLS", "true")
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
                        "os.dup2(fd,1);os.dup2(fd,2);os.close(fd);"
                        "sys.exit(subprocess.run(sys.argv[2:]).returncode)")
            # Build backends may write build/ and *.egg-info into the project.
            # Native hook trust hashes the installed plugin payload, so even a
            # noneditable build must operate outside that published payload.
            project = release / "build"
            if project.exists():
                shutil.rmtree(project)
            project.mkdir()
            try:
                for path in package_files(source):
                    target = project / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                run([sys.executable, "-I", "-B", "-c", redirect, log, uv, "sync",
                     "--project", str(project), "--locked", "--no-editable", "--no-dev",
                     "--python", sys.executable], "", timeout=budget(540, 255), env=env).checked_stdout()
            finally:
                shutil.rmtree(project)
            Path(log).unlink()
        with preparing("installed-readback"):
            scripts = readback(python, source)
        with preparing("candidate-validation"):
            validation = probe(python, scripts, lambda argv, **kw: run(argv, "", **kw).checked_stdout())
        with preparing("release-publication"):
            temporary = release / "runtime.json.tmp"
            temporary.write_text(json.dumps({"key": key, "python": str(python),
                                             "validation": validation}))
            os.replace(temporary, receipt)
            publish_entry(python, scripts, release)
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
        return target
    temporary = release / "entry.py.tmp"
    temporary.write_text(text, encoding="utf-8")
    try:
        os.link(temporary, target)
    except FileExistsError:
        if target.read_text(encoding="utf-8") != text:
            raise ValueError("immutable release entry differs")
    finally:
        temporary.unlink()
    return target


def main():
    source = Path(__file__).resolve().parents[1]
    data = Path(sys.argv[1])
    python, scripts, release = ensure_runtime(source, data)
    entry = release / "entry.py"
    exec_or_wait([str(python), str(entry), *sys.argv[2:]], dict(os.environ))


if __name__ == "__main__":
    try:
        with preparing("bootstrap"):
            main()
    except Exception:
        raise SystemExit(1)
