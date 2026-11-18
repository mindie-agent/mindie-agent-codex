"""Thin MindIE profile, sharing, doctor, update, and release-metadata CLI.
[Role] Initialize adapter/engine config, persist sharing, delegate `doctor`/`update` to `mindie_harness`, and validate release metadata.
[Invariants] Config writes are atomic; `transcript_adapter` points at `codex_transcript.py`; every interface code path and test id must resolve.
[Timeouts & Locks] Doctor/update deadlines come from `mindie_harness`; no local locks.
[Side-Effect Policy] Only install/configure write; status and metadata are read-only.
"""
from __future__ import annotations
import ast, importlib.util, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
_NO_WIN = 0x08000000 if os.name == "nt" else 0
REPOSITORY = "mindie-agent/mindie-agent-codex"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
def _version(root: Path) -> str: return str(json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]) if (root / ".codex-plugin/plugin.json").is_file() else "0.1.38"
def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent); temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True); stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
def ensure_initialized(profile, *, python: Path, scripts_dir: Path, release_dir: Path):
    """Write the canonical adapter and engine documents once; return both."""
    config, engine = Path(profile.config_path), Path(profile.config_path).with_name(Path(profile.config_path).stem + ".engine.json")
    if config.is_file() and engine.is_file():
        return json.loads(config.read_text(encoding="utf-8")), json.loads(engine.read_text(encoding="utf-8"))
    version = _version(Path(scripts_dir).parent); validation = {"schema": "mindie-candidate-check/1", "status": "validated", "native_version": version}
    community = config.parent / "mindie-community.json"; admission = config.parent / (config.stem + ".admission.sqlite3")
    engine_doc = {"root": str(Path(os.environ.get("MINDIE_KNOWLEDGE_ROOT") or Path(profile.data_dir) / "knowledge")), "domain": "vllm-ascend", "admission_path": str(admission), "transcript_adapter": str(Path(scripts_dir) / "codex_transcript.py"), "community_config": str(community), "product_validation": validation}
    existing = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}; adapter = {**existing, "host": "codex", "python": str(python), "engine_config": str(engine), "community_config": str(community), "consent_config": str(config.parent / "mindie-consent.json"), "admission_path": str(admission), "runtime_scripts": str(scripts_dir), "source_root": str(Path(scripts_dir).parent), "component_release": str(release_dir), "product_validation": validation, "domain_configs": existing.get("domain_configs", {})}
    if redactor := (shutil.which("gitleaks") or os.environ.get("MINDIE_REDACTOR_EXECUTABLE")):
        engine_doc["redactor_executable"] = redactor
    _atomic(engine, engine_doc); _atomic(config, adapter); return adapter, engine_doc
def _profile(config: str | None):
    from mindie_harness.paths import ProfileRef
    return ProfileRef.resolve("codex", config_path=config or os.environ.get("MINDIE_AGENT_CONFIG"), plugin_root=SCRIPTS.parent)
def _has(relative: str, root: Path) -> bool:
    file_part, _, symbol = relative.partition("::")
    if not symbol:
        file_part, _, symbol = file_part.partition(":")
    path = root / (Path(file_part).name if file_part.startswith("tests/") else file_part)
    if not path.is_file():
        return False
    if not symbol:
        return True
    node = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for name in symbol.split("."):
        node = next((child for child in ast.iter_child_nodes(node) if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == name), None)
        if node is None:
            return False
    return True
def release_metadata(source: Path, tests: Path | None, revision: str | None) -> dict:
    from mindie_knowledge.releases import validate_compatibility
    if revision is not None and not SHA_RE.fullmatch(revision):
        raise ValueError("revision must be a 40-character lowercase SHA")
    version = _version(source); compatibility = json.loads((source / "harness-compatibility.json").read_text(encoding="utf-8")); validate_compatibility(compatibility, harness_name="codex")
    for entry in compatibility["interface_registry"].values():
        if any(not _has(path, source) for path in entry["code_paths"]) or (tests is not None and any(not _has(test, tests) for test in entry["test_ids"])):
            raise ValueError("harness-compatibility code_paths/test_ids do not resolve")
    servers = json.loads((source / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"].values()
    seed = {argv[index + 1] for server in servers for argv in [server.get("args", [])] for index, token in enumerate(argv[:-1]) if token == "--seed"}
    if seed != {version}:
        raise ValueError("inline .mcp.json seed does not match the plugin version")
    return {"schema": "mindie-plugin-release/1", "version": version, "source": {"repository": REPOSITORY, "sha": revision or "0" * 40}, "compatibility": compatibility}
def _flags(argv: list[str], spec: dict[str, bool]) -> dict:
    values, index = {}, 0
    while index < len(argv):
        token = argv[index]
        if token not in spec: index += 1
        elif spec[token]: values[token] = True; index += 1
        elif index + 1 < len(argv): values[token] = argv[index + 1]; index += 2
        else: raise SystemExit(f"{token} requires a value")
    return values
def _install(argv: list[str]) -> dict:
    flags = _flags(argv, {"--knowledge-python": False, "--config": False, "--root": False, "--domain": False, "--no-public-feed": True})
    profile = _profile(flags.get("--config"))
    existing = json.loads(Path(profile.config_path).read_text(encoding="utf-8")) if Path(profile.config_path).is_file() else {}
    chosen_py = flags.get("--knowledge-python") or (existing.get("python") if isinstance(existing.get("python"), str) and Path(existing["python"]).is_file() else sys.executable)
    adapter, engine = ensure_initialized(profile, python=Path(chosen_py), scripts_dir=SCRIPTS, release_dir=SCRIPTS.parent)
    if root := flags.get("--root"):
        engine["root"] = str(Path(root).expanduser().absolute())
    if domain := flags.get("--domain"):
        engine["domain"] = domain
    _atomic(Path(profile.config_path).with_name(Path(profile.config_path).stem + ".engine.json"), engine)
    return {"status": "configured", "config": str(profile.config_path), "engine_config": str(adapter["engine_config"]), "admission_path": adapter["admission_path"], "transcript_adapter": engine["transcript_adapter"], "domain": engine["domain"]}
def _sharing(argv: list[str]) -> dict:
    flags = _flags(argv, {"--config": False, "--status": True, "--enable": True, "--local-only": True, "--disable": True, "--project-root": False, "--repository": False, "--fork": False, "--account": False})
    if sum(bool(flags.get(key)) for key in ("--status", "--enable", "--local-only", "--disable")) > 1:
        raise SystemExit("conflicting sharing flags: choose at most one of --status, --enable, --local-only, --disable")
    from mindie_harness.sharing import configure_sharing, sharing_status
    profile = _profile(flags.get("--config"))
    if flags.get("--status") or not any(key in flags for key in ("--enable", "--local-only", "--disable")):
        return sharing_status(profile)
    choice = "contribute" if flags.get("--enable") else "local-only" if flags.get("--local-only") else "disabled"
    return configure_sharing(profile, choice=choice, project_roots=[flags["--project-root"]] if flags.get("--project-root") else [], repository=flags.get("--repository"), fork=flags.get("--fork"), account=flags.get("--account"))
def _doctor(argv: list[str]) -> int:
    flags = _flags(argv, {"--probe-remote": True, "--cwd": False, "--timeout": False}); from mindie_harness.api import run_host
    forwarded = ["--host", "codex", "doctor"] + (["--probe-remote"] if flags.get("--probe-remote") else []) + (["--cwd", flags["--cwd"]] if flags.get("--cwd") else []) + (["--timeout", flags["--timeout"]] if flags.get("--timeout") else [])
    return run_host(forwarded)
def _update(argv: list[str]) -> dict:
    from mindie_harness.runtime.wheelhouse import update_runtime
    flags = _flags(argv, {"--config": False, "--force": True, "--offline": True, "--channel-base-url": False, "--background": True})
    return update_runtime(_profile(flags.get("--config")), force=bool(flags.get("--force")), offline=bool(flags.get("--offline")), channel_base_url=flags.get("--channel-base-url"), background=bool(flags.get("--background")))
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: setup.py {install|init|configure|sharing|doctor|update|release-metadata} ...", file=sys.stderr); return 2
    if importlib.util.find_spec("mindie_harness") is None:
        from with_runtime import selected_python
        runtime_py = selected_python()
        if Path(runtime_py).resolve() != Path(sys.executable).resolve():
            return subprocess.run([runtime_py, "-I", "-X", "utf8", str(Path(__file__).resolve()), *args], creationflags=_NO_WIN).returncode
    command, rest = args[0], args[1:]
    if command in {"install", "init", "configure"}:
        print(json.dumps(_install(rest), sort_keys=True)); return 0
    if command == "sharing":
        print(json.dumps(_sharing(rest), sort_keys=True)); return 0
    if command == "doctor":
        return _doctor(rest)
    if command == "update":
        print(json.dumps(_update(rest), sort_keys=True)); return 0
    if command == "release-metadata":
        flags = _flags(rest, {"--source": False, "--tests": False, "--revision": False})
        print(json.dumps(release_metadata(Path(flags.get("--source") or SCRIPTS.parent).resolve(), Path(flags["--tests"]).resolve() if flags.get("--tests") else None, flags.get("--revision")), sort_keys=True)); return 0
    raise SystemExit(f"unknown setup command: {command}")
if __name__ == "__main__":
    raise SystemExit(main())
