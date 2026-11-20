"""Cold-start runtime builder, component-floor preserver, and entry publisher.
[Role] Build `data/runtimes/<key>` with locked `uv sync`, preserve newer component floors across all 9 runtime packages, publish `releases/<version>/runtime.json`, and write durable entries while leaving runtime generation GC (`Active + Rollback <= 2`) solely to `mindie-harness`.
[Invariants] Installer output goes to stderr; stdout stays clean; never deletes `<data>/runtimes/codex/` directories.
[Timeouts & Locks] Hold `.prepare-codex.lock` across one bounded `540s` build.
[Side-Effect Policy] Write only under `<data>` and an owned temporary build directory.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib, json, os, shutil, subprocess, sys, sysconfig, tempfile, time
from pathlib import Path
_NO_WIN = 0x08000000 if os.name == "nt" else 0
_PROBE = "import importlib.util,json,pathlib;root=pathlib.Path(importlib.util.find_spec('mindie_codex').origin).parent;print(json.dumps({'root':str(root),'version':json.loads((root/'.codex-plugin/plugin.json').read_text(encoding='utf-8'))['version']}))"
_INVENTORY = "import importlib.metadata as m,json;rows={};[rows.__setitem__((d.metadata.get('Name') or '').strip().lower(),{'version':d.version,'repository':(json.loads(d.read_text('direct_url.json') or '{}').get('url')),'revision':(json.loads(d.read_text('direct_url.json') or '{}').get('vcs_info') or {}).get('commit_id')}) for d in m.distributions() if ((d.metadata.get('Name') or '').strip().lower().startswith('mindie-') or (d.metadata.get('Name') or '').strip().lower() in ('remote-dev','npu-top'))];print(json.dumps(rows))"
def _config_file() -> Path:
    if explicit := os.environ.get("MINDIE_AGENT_CONFIG"): return Path(explicit).expanduser()
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get("MINDIE_CONFIG_HOME") or base / "mindie-agent").expanduser() / "codex.json"
def _version(source: Path) -> str:
    version = json.loads((source / ".codex-plugin/plugin.json").read_text(encoding="utf-8")).get("version")
    if not isinstance(version, str) or not version: raise ValueError("native plugin version is unavailable")
    return version
def _key(source: Path, version: str) -> str: return hashlib.sha256(f"{source}:{version}:{sys.implementation.cache_tag}:{sysconfig.get_platform()}".encode()).hexdigest()
def _read(path: Path):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except TimeoutError: raise
    except (OSError, ValueError): return None
def _write_atomic_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True); fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent); tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream: stream.write(text)
        os.replace(tmp, target)
    finally: tmp.unlink(missing_ok=True)
def _atomic(path: Path, value: dict) -> None: _write_atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
@contextmanager
def _lock(path: Path, *, timeout: float = 600.0):
    path.parent.mkdir(parents=True, exist_ok=True); deadline = time.monotonic() + timeout
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0); handle.write(b"0"); handle.flush()
            while True:
                try: handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1); break
                except OSError:
                    if time.monotonic() >= deadline: raise TimeoutError("timed out waiting for codex runtime lock") from None
                    time.sleep(0.2)
            try: yield
            finally: handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            while True:
                try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise TimeoutError("timed out waiting for codex runtime lock") from None
                    time.sleep(0.2)
            try: yield
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
def _env(runtime: Path, data: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(UV_PROJECT_ENVIRONMENT=str(runtime), UV_CACHE_DIR=env.get("UV_CACHE_DIR") or str(data / ".uv-cache"), UV_SYSTEM_CERTS="true", UV_NO_PROGRESS="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="core.longpaths", GIT_CONFIG_VALUE_0="true", GIT_CONFIG_KEY_1="core.autocrlf", GIT_CONFIG_VALUE_1="false")
    return env
def _run(argv: list[str], *, env: dict[str, str], timeout: float) -> None:
    if subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr, timeout=timeout, check=False, creationflags=_NO_WIN).returncode: raise RuntimeError(f"{Path(argv[0]).name} failed")
def _probe(python: Path, code: str, *, timeout: float = 60.0) -> dict:
    res = subprocess.run([str(python), "-I", "-X", "utf8", "-c", code], capture_output=True, timeout=timeout, check=False, creationflags=_NO_WIN)
    val = json.loads(res.stdout.decode("utf-8")) if res.returncode == 0 else None
    if not isinstance(val, dict): raise ValueError("runtime probe failed")
    return val
def _scripts(python: Path, version: str) -> Path:
    val = _probe(python, _PROBE); root = Path(val["root"]); manifest = _read(root / ".codex-plugin/plugin.json")
    if val.get("version") != version or not isinstance(manifest, dict) or manifest.get("version") != version or not (root / "scripts/entry.py").is_file(): raise ValueError("installed runtime differs")
    return root / "scripts"
def _semver(v: str) -> tuple[int, ...]: return tuple(int("".join(c for c in tok if c.isdigit()) or 0) for tok in str(v).split(".")[:3])
def _uv() -> str:
    if uv := shutil.which("uv"): return uv
    raise FileNotFoundError("uv is required to prepare MindIE's locked runtime")
def _restore_floors(old: Path, new: Path) -> None:
    if not old.is_file() or not new.is_file(): return
    try: before = _probe(old, _INVENTORY, timeout=30.0)
    except TimeoutError: raise
    except (OSError, ValueError, subprocess.TimeoutExpired): return
    after = _probe(new, _INVENTORY, timeout=30.0); env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}; upgraded = False
    for name, row in before.items():
        cur = after.get(name) or {}
        if not isinstance(row, dict) or name in {"mindie-codex", "mindie-agent-codex"} or _semver(row.get("version", "0")) <= _semver(cur.get("version", "0")): continue
        repo, rev = row.get("repository"), row.get("revision")
        spec = f"{name} @ git+{repo if '://' in repo else 'https://github.com/' + repo}@{rev}" if (isinstance(repo, str) and repo and isinstance(rev, str) and rev) else (f"{name} @ {repo}" if isinstance(repo, str) and repo.endswith(".whl") else f"{name}>={row['version']}")
        _run([_uv(), "pip", "install", "--python", str(new), "--no-deps", spec], env=env, timeout=240.0); upgraded = True
    final = _probe(new, _INVENTORY, timeout=30.0) if upgraded else after
    if any(isinstance(row, dict) and name not in {"mindie-codex", "mindie-agent-codex"} and _semver(final.get(name, {}).get("version", "0")) < _semver(row.get("version", "0")) for name, row in before.items()): raise RuntimeError("component floor verification failed")
def _build(source: Path, data: Path, runtime: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mindie-build-") as directory:
        project = Path(directory) / "project"; shutil.copytree(source, project, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "build", "dist", "releases", "runtimes"))
        cmd = [_uv(), "--native-tls", "sync", "--project", str(project), "--locked", "--no-editable", "--no-dev", "--python", sys.executable]
        try: _run(cmd, env=_env(runtime, data), timeout=540.0)
        except RuntimeError: time.sleep(1.5); _run(cmd, env=_env(runtime, data), timeout=540.0)
def _publish(python: Path, scripts: Path, release: Path, data: Path, source: Path) -> None:
    launcher = (Path(scripts) / "hook_launcher.py").read_text(encoding="utf-8")
    entry = ("import os,subprocess,sys\n" f"python={str(python)!r}\nentry={str(Path(scripts)/'entry.py')!r}\nrelease={str(release)!r}\nsource={str(source)!r}\n"
             "env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}\ncmd=[python,'-I','-X','utf8',entry,'--release',release,'--source',source,*sys.argv[1:]]\n"
             "if os.name=='nt':\n raise SystemExit(subprocess.run(cmd,env=env,stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr,creationflags=0x08000000).returncode)\nos.execve(python,cmd,env)\n")
    for target, text in ((release / "entry.py", entry), (release / "hook_launcher.py", launcher), (data / "hook_launcher.py", launcher)): _write_atomic_text(target, text)
def _has_harness(python: Path) -> bool:
    try: return _probe(python, "import importlib.util,json;print(json.dumps({'ok':importlib.util.find_spec('mindie_harness') is not None}))", timeout=30.0).get("ok") is True
    except TimeoutError: raise
    except (OSError, ValueError, subprocess.TimeoutExpired): return False
def ensure_runtime(source, data) -> tuple[Path, Path, Path]:
    source, data = Path(source).resolve(), Path(data).absolute(); version = _version(source); key = _key(source, version)
    release, runtime = data / "releases" / version, data / "runtimes" / key[:24]; python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    data.mkdir(parents=True, exist_ok=True); check = {"schema": "mindie-candidate-check/1", "status": "validated", "native_version": version}
    with _lock(data / ".prepare-codex.lock"):
        cfg_path = _config_file(); before, receipt = _read(cfg_path), _read(release / "runtime.json")
        if before is None and os.environ.get("MINDIE_CHANNEL_BASE_URL") and (seed := source / "assets/mindie-harness-seed.pyz").is_file():
            subprocess.run([sys.executable, "-I", "-X", "utf8", str(seed), "--host", "codex", "--plugin-root", str(source), "--config", str(cfg_path), "status"], stdin=subprocess.DEVNULL, capture_output=True, timeout=240.0, check=False, creationflags=_NO_WIN); before = _read(cfg_path)
        old = Path(before["python"]) if isinstance(before, dict) and isinstance(before.get("python"), str) else None
        if old is not None and old.is_file() and isinstance(before, dict) and (str(before.get("channel_base_url") or "").strip() or os.environ.get("MINDIE_CHANNEL_BASE_URL")) and (source / "scripts/entry.py").is_file():
            adopted = (isinstance(receipt, dict) and receipt.get("wheelhouse_adopted") is True and receipt.get("python") == str(old)) or _has_harness(old)
            if adopted:
                if not (isinstance(receipt, dict) and receipt.get("wheelhouse_adopted") is True and receipt.get("python") == str(old)):
                    _atomic(release / "runtime.json", {"key": key, "python": str(old), "source": str(source), "wheelhouse_adopted": True, "validation": check})
                _publish(old, source / "scripts", release, data, source); return old, source / "scripts", release
        if isinstance(receipt, dict) and not receipt.get("wheelhouse_adopted") and isinstance(receipt.get("python"), str) and Path(receipt["python"]).is_file():
            try: scripts = _scripts(Path(receipt["python"]), version)
            except TimeoutError: raise
            except (OSError, ValueError, subprocess.TimeoutExpired): pass
            else: _publish(Path(receipt["python"]), scripts, release, data, source); return Path(receipt["python"]), scripts, release
        _build(source, data, runtime)
        if old is not None: _restore_floors(old, python)
        scripts = _scripts(python, version); _atomic(release / "runtime.json", {"key": key, "python": str(python), "source": str(source), "validation": check})
        _publish(python, scripts, release, data, source); return python, scripts, release
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args: raise SystemExit("usage: bootstrap.py <data-dir> [--source <source>] [role ...]")
    data, source = Path(args.pop(0)), Path(__file__).resolve().parents[1]
    if "--source" in args: idx = args.index("--source"); source = Path(args[idx + 1]).resolve(); del args[idx:idx + 2]
    python, scripts, release = ensure_runtime(source, data); cmd = [str(python), "-I", "-X", "utf8", str(scripts / "entry.py"), "--release", str(release), "--source", str(source), *args]
    return subprocess.run(cmd, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, creationflags=_NO_WIN).returncode if os.name == "nt" else os.execv(str(python), cmd)
if __name__ == "__main__": raise SystemExit(main())
