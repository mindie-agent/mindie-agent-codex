"""Prepare this plugin's uv-locked, noneditable runtime; never select an update."""
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

from product_contract import package_files, source_identity, probe, identity, validate_receipt, native_version
from bootstrap_lock import exclusive_lock
from diagnostic_fallback import _private_mkdir
from bounded_process import run


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
    value = json.loads(run([str(python), "-I", "-c", code], "").checked_stdout())
    if value["version"] != native_version(source):
        raise ValueError("installed distribution differs from native plugin version")
    installed = Path(value["root"])
    if Path(value["prefix"]).absolute() != python.parent.parent.absolute():
        raise ValueError("runtime interpreter identity differs")
    for path in package_files(source):
        target = installed / path.relative_to(source)
        if path.is_symlink() or not target.is_file() or target.read_bytes() != path.read_bytes():
            raise ValueError("installed adapter differs from native plugin")
    return installed / "scripts"


def ensure_runtime(source, data):
    with preparing("source-identity"):
        source, data = Path(source).resolve(), Path(data).absolute()
        version = native_version(source)
        key = runtime_key(source)
        release = data / "releases" / version
        _private_mkdir(release, parents=True, exist_ok=True)
    runtime = data / "runtimes" / key
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    receipt = release / "runtime.json"
    with exclusive_lock(release / "prepare.lock"):
        if receipt.exists():
            existing = json.loads(receipt.read_text(encoding="utf-8"))
            if existing.get("key") != key or existing.get("python") != str(python):
                raise ValueError("immutable plugin version has different source or Python ABI")
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
        # uv locks its target environment. An interrupted, unpublished build
        # can be synced again; a completed runtime above is never repaired.
        with preparing("locked-sync"):
            # Failed installer output stays private and separate from native
            # Hook output. It is diagnostic evidence, never readiness authority.
            descriptor, log = tempfile.mkstemp(prefix="prepare-", suffix=".log", dir=release)
            os.close(descriptor)
            # Redirect before exec so installer progress survives caller loss;
            # the existing owned runner still contains uv and all descendants.
            redirect = ("import os,sys;"
                        "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_APPEND);"
                        "os.dup2(fd,1);os.dup2(fd,2);os.close(fd);"
                        "os.execv(sys.argv[2],sys.argv[2:])")
            run([sys.executable, "-I", "-c", redirect, log, uv, "sync",
                 "--project", str(source), "--locked", "--no-editable", "--no-dev",
                 "--python", sys.executable], "", env=env).checked_stdout()
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
        "os.execve(python,[python,'-I',entry,'--release',release,*sys.argv[1:]],env)\n"
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
    os.execve(str(python), [str(python), str(entry), *sys.argv[2:]], dict(os.environ))


if __name__ == "__main__":
    try:
        with preparing("bootstrap"):
            main()
    except Exception:
        raise SystemExit(1)
