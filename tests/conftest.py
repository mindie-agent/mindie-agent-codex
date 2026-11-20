"""Isolated, fast host-environment fixtures and zero-.venv-mutation pre-0.1.5 test shim."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_HARNESS_PYTHONPATH: tuple[str, ...] = tuple(
    str(Path(entry).resolve())
    for entry in (os.environ.get("PYTHONPATH") or "").split(os.pathsep)
    if entry.strip() and (Path(entry.strip()) / "mindie_harness" / "__init__.py").is_file()
)

_COMPAT_SHIM_CODE = '''"""Test-only pre-0.1.5 mindie_harness compatibility shim (auto-disabled once >=0.1.5 is active)."""
from __future__ import annotations
from contextlib import closing
import hashlib, importlib.machinery, importlib.util, json, os, shutil, sys, time, types
from pathlib import Path

def install() -> bool:
    if importlib.util.find_spec("mindie_harness") is None:
        return False
    installed = False
    if "mindie_harness.runtime.knowledge_binding" not in sys.modules and importlib.util.find_spec("mindie_harness.runtime.knowledge_binding") is None:
        installed = True
        mod = types.ModuleType("mindie_harness.runtime.knowledge_binding")
        mod.__spec__ = importlib.machinery.ModuleSpec("mindie_harness.runtime.knowledge_binding", None)

        def _noop_adapter_path(data_dir: Path) -> Path:
            target = Path(data_dir) / "state/noop_transcript_adapter.py"
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    "from dataclasses import dataclass\\n"
                    "@dataclass(frozen=True)\\nclass FileIdentity:\\n path: str = ''\\n"
                    "def identify(path): return None\\n"
                    "def read_material(path, start, **kw): return {'status': 'unchanged', 'start': start, 'end': start, 'digest': '0'*64, 'text': '', 'records': 0, 'skipped_records': 0, 'partial': False, 'more': False}\\n",
                    encoding="utf-8",
                    newline="\\n",
                )
            return target.resolve()

        def _resolve_transcript_adapter(profile, plugin_root: Path | None, prev_eng: dict) -> Path:
            candidates = (
                "codex_transcript.py", "transcript.py", "kimi_transcript.py",
                "dsh_transcript.py", "agy_transcript.py", "wb_transcript.py", "opencode_transcript.py",
            )
            roots = ([Path(plugin_root).resolve()] if plugin_root else []) + [Path(profile.plugin_root).resolve()]
            for base in roots:
                for sub in ("scripts", "plugins/mindie-agent/scripts", "."):
                    for name in candidates:
                        cand = (base / sub / name).resolve()
                        if cand.is_file():
                            return cand
            prev = prev_eng.get("transcript_adapter")
            if isinstance(prev, str) and Path(prev).is_absolute() and Path(prev).is_file():
                return Path(prev).resolve()
            return _noop_adapter_path(Path(profile.data_dir))

        def ensure_knowledge_binding(
            profile,
            *,
            plugin_root=None,
            python_executable=None,
            install_redactor=False,
            retire_previous=False,
            include_default_feed=False,
            already_locked=False,
        ):
            from mindie_harness.atomic import atomic_write_json, read_json_safe
            from mindie_harness.locks import FileLock, LockTimeoutError
            from mindie_knowledge.loop import domain_catalog
            from mindie_knowledge.loop.cli import config_at, validate_config
            from mindie_knowledge.loop.handoff import prepare_schema
            from mindie_knowledge.loop.lifecycle import inspect_retirement

            cfg_path = Path(profile.config_path)
            lock = None
            if not already_locked:
                lock = FileLock(cfg_path.with_name(f".{cfg_path.name}.update.lock"), exclusive=True, timeout=5.0)
                try:
                    lock.acquire()
                except LockTimeoutError:
                    return {"status": "deferred", "reason": "lock_timeout", "reportable": False}
            try:
                existing = dict(read_json_safe(cfg_path) or {})
                old_ep = existing.get("engine_config")
                prev_eng = dict(read_json_safe(Path(old_ep)) or {}) if isinstance(old_ep, str) and Path(old_ep).is_file() else dict(read_json_safe(cfg_path.with_name(f"{cfg_path.stem}.engine.json")) or {})
                env_root = os.environ.get("MINDIE_KNOWLEDGE_ROOT")
                prev_root = prev_eng.get("root")
                kroot = str(Path(env_root.strip() if isinstance(env_root, str) and env_root.strip() else (prev_root if isinstance(prev_root, str) and prev_root.strip() else (Path(profile.data_dir) / "knowledge"))).expanduser().resolve())
                default_domain = "vllm-ascend"
                consent = Path(profile.consent_path).resolve()
                community = (cfg_path.parent / "mindie-community.json").resolve()
                admission = Path(profile.admission_db).resolve()
                python_exe = Path(python_executable or existing.get("python") or sys.executable)
                redactor = shutil.which("gitleaks") or os.environ.get("MINDIE_REDACTOR_EXECUTABLE") or str((Path(profile.data_dir) / "cache/gitleaks/8.30.1" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")).resolve())
                transcript_path = _resolve_transcript_adapter(profile, plugin_root, prev_eng)
                default_engine = {
                    "root": kroot,
                    "domain": default_domain,
                    "domain_catalog": domain_catalog.SCHEMA,
                    "capture_mode": "public-transcript",
                    "transcript_adapter": str(transcript_path),
                    "redactor_executable": str(Path(redactor).resolve()),
                    "admission_path": str(admission),
                    "community_config": str(community),
                    "product_validation": {"schema": "mindie-candidate-check/1", "status": "validated", "python": str(python_exe)},
                }
                if isinstance(prev_eng.get("feeds"), list) and prev_eng["feeds"]:
                    default_engine["feeds"] = list(prev_eng["feeds"])
                elif include_default_feed:
                    default_engine["feeds"] = [domain_catalog.get_domain_config(default_domain).to_feed()]
                validate_config(default_engine)

                engines_dir = (cfg_path.parent / "engines" / profile.host).resolve()
                authorities_dir = (cfg_path.parent / "authorities" / profile.host).resolve()
                text = json.dumps(default_engine, ensure_ascii=False, sort_keys=True, indent=2) + "\\n"
                default_engine_path = engines_dir / hashlib.sha256(text.encode("utf-8")).hexdigest()[:20] / f"{default_domain}.engine.json"
                if not default_engine_path.is_file() or default_engine_path.read_text(encoding="utf-8") != text:
                    default_engine_path.parent.mkdir(parents=True, exist_ok=True)
                    default_engine_path.write_text(text, encoding="utf-8", newline="\\n")
                prepare_schema(default_engine_path)

                sanitized = {}
                for dom, p_str in (existing.get("domain_configs") or {}).items():
                    if not isinstance(dom, str) or dom == default_domain or not isinstance(p_str, str) or not Path(p_str).is_file():
                        continue
                    try:
                        if inspect_retirement(Path(p_str)) is None and config_at(Path(p_str)).get("domain") == dom:
                            sanitized[dom] = str(Path(p_str).resolve())
                    except Exception:
                        continue

                prep = domain_catalog.prepare(
                    {"engine_config": str(default_engine_path), "domain_configs": sanitized},
                    default_engine,
                    config_directory=str(engines_dir),
                    authority_directory=str(authorities_dir),
                    consent_config=str(consent),
                    public_feeds=True,
                )
                new_routes = dict(prep["domain_configs"])
                new_routes[default_domain] = str(default_engine_path)

                retirement_report = None
                if retire_previous:
                    retire_prev_routes = {}
                    for dom, p_str in (existing.get("domain_configs") or {}).items():
                        if isinstance(dom, str) and isinstance(p_str, str) and Path(p_str).is_file():
                            try:
                                if config_at(p_str).get("domain") == dom:
                                    retire_prev_routes[dom] = str(Path(p_str).resolve())
                            except Exception:
                                continue
                    retire_prev_doc = {"domain_configs": retire_prev_routes}
                    if isinstance(old_ep, str) and Path(old_ep).is_file():
                        try:
                            config_at(old_ep)
                            retire_prev_doc["engine_config"] = str(Path(old_ep).resolve())
                        except Exception:
                            pass
                    retirement_report = domain_catalog.retire_changed(
                        retire_prev_doc,
                        {"engine_config": str(default_engine_path), "domain_configs": new_routes},
                        timeout=15.0,
                        include_default=True,
                    )
                    if retirement_report.get("status") == "deferred":
                        return {"status": "deferred", "reason": "knowledge_service_busy", "reportable": False, "retirement": retirement_report}

                binding_fields = {
                    "host": profile.host,
                    "engine_config": str(default_engine_path),
                    "domain_configs": new_routes,
                    "community_config": str(community),
                    "consent_config": str(consent),
                    "admission_path": str(admission),
                }
                if not already_locked:
                    merged = dict(existing)
                    merged.setdefault("python", str(python_exe))
                    merged.setdefault("source_root", str(Path(plugin_root or profile.plugin_root).resolve()))
                    merged.update(binding_fields)
                    merged["updated_at"] = time.time()
                    atomic_write_json(cfg_path, merged)
                return {
                    "status": "configured",
                    "engine_config": str(default_engine_path),
                    "domain_configs": new_routes,
                    "domains": sorted(new_routes),
                    "transcript_adapter": str(transcript_path),
                    "redactor_executable": str(Path(redactor).resolve()),
                    "binding_fields": binding_fields,
                    "retirement": retirement_report,
                    "reportable": False,
                }
            finally:
                if lock is not None:
                    lock.release()

        def run_stop_capture(profile, event, *, plugin_root=None, timeout_sec=6.0):
            from mindie_harness.atomic import read_json_safe
            deadline = time.monotonic() + float(timeout_sec)
            if not isinstance(event, dict):
                return {"stage": "inert", "reason": "not-stop"}
            if event.get("stop_hook_active", False) is not False:
                return {"stage": "inert", "reason": "recursive-stop"}
            cfg_doc = read_json_safe(Path(profile.config_path)) or {}
            comm_path = Path(cfg_doc["community_config"]) if isinstance(cfg_doc.get("community_config"), str) and os.path.isabs(cfg_doc["community_config"]) else (Path(profile.config_path).parent / "mindie-community.json")
            comm = read_json_safe(comm_path) or {}
            if comm.get("schema") != "mindie-community-config/1" or comm.get("enabled") is not True:
                return {"stage": "inert", "reason": "sharing-disabled"}
            cons_path = Path(comm.get("consent_config") or cfg_doc.get("consent_config") or profile.consent_path)
            cons = read_json_safe(cons_path) or {}
            if cons.get("schema") != "mindie-consent/1" or cons.get("choice") not in {"contribute", "local-only"} or not isinstance(cons.get("choice_revision"), str) or not cons["choice_revision"].strip():
                return {"stage": "inert", "reason": "sharing-disabled"}
            roots = [Path(r).expanduser().resolve() for r in (comm.get("project_roots") or []) if isinstance(r, str) and os.path.isabs(r)]
            if not roots:
                return {"stage": "inert", "reason": "sharing-disabled"}
            cwd_cand = next((event.get(k) for k in ("cwd", "workspace", "workspace_root", "project_root", "directory") if isinstance(event.get(k), str) and os.path.isabs(event[k])), None)
            if not cwd_cand:
                return {"stage": "inert", "reason": "missing-cwd"}
            cand_norm = os.path.normcase(str(Path(cwd_cand).resolve()))
            matched_root = next((str(r) for r in roots if os.path.splitdrive(cand_norm)[0] == os.path.splitdrive(os.path.normcase(str(r)))[0] and os.path.commonpath((cand_norm, os.path.normcase(str(r)))) == os.path.normcase(str(r))), None)
            if matched_root is None:
                return {"stage": "inert", "reason": "out-of-scope"}
            session_id = event.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                return {"stage": "rejected", "reason": "missing-identity"}
            session_id = session_id.strip()
            ep_raw = cfg_doc.get("engine_config")
            eng_path = Path(ep_raw) if isinstance(ep_raw, str) and ep_raw.strip() else None
            if eng_path is None or not eng_path.is_file():
                ens = ensure_knowledge_binding(profile, plugin_root=plugin_root, already_locked=False)
                if ens.get("status") != "configured":
                    return {"stage": "unavailable", "reason": "configuration"}
                eng_path = Path(ens["engine_config"])
            from mindie_knowledge.loop.activation import Admission
            from mindie_knowledge.loop.cli import config_at
            from mindie_knowledge.loop.handoff import accept_stop
            eng = config_at(eng_path)
            admission = Admission(eng["admission_path"])
            inspected = admission.inspect(session_id, deadline=deadline)
            if inspected.get("status") == "inactive":
                return {"stage": "inert", "reason": "revoked"}
            lease = admission.active_lease(session_id, deadline=deadline)
            if (lease is None or not lease.get("entry_enabled")) and inspected.get("status") == "missing":
                lease = admission.activate(session_id, project_root=matched_root, root_session=session_id, not_before=0.0)
            if lease is None or not lease.get("token") or not lease.get("entry_enabled"):
                return {"stage": "inert", "reason": "not-activated"}
            fwd = dict(event, hook_event_name="Stop", session_id=session_id)
            fwd.setdefault("harness", profile.host)
            if not fwd.get("identity_kind"):
                if isinstance(fwd.get("turn_id"), str) and fwd["turn_id"].strip() and not fwd.get("event_id"):
                    fwd["identity_kind"] = "turn"
                elif isinstance(fwd.get("event_id"), str) and fwd["event_id"].strip():
                    fwd["identity_kind"] = "notification"
            if fwd.get("identity_kind") == "notification":
                fwd.pop("last_assistant_message", None)
            fwd["mindie_activation"] = str(lease["token"])
            return accept_stop(eng_path, fwd, deadline=deadline)

        mod.ensure_knowledge_binding = ensure_knowledge_binding
        mod.run_stop_capture = run_stop_capture
        sys.modules["mindie_harness.runtime.knowledge_binding"] = mod
        import mindie_harness.runtime as rt_pkg
        setattr(rt_pkg, "knowledge_binding", mod)

    import mindie_harness
    ver_parts = tuple(int(p) if p.isdigit() else 0 for p in str(getattr(mindie_harness, "__version__", "0.0.0")).split(".")[:3])
    if ver_parts < (0, 1, 5):
        installed = True
        from mindie_harness.mcp import router as router_mod
        from mindie_harness.paths import HarnessError
        orig_validate = router_mod.MCPRouter._validate_and_clean_args
        if not getattr(orig_validate, "_pre015_patched", False):
            def _patched_validate(self, name: str, raw_args):
                tool_def = self._tool_map.get(name)
                schema = (tool_def.get("inputSchema") if isinstance(tool_def, dict) else None) or {}
                props = schema.get("properties") if isinstance(schema, dict) else None
                if not (name.startswith("remote_") and isinstance(raw_args, dict) and "session_id" in raw_args and isinstance(props, dict) and "session_id" in props):
                    return orig_validate(self, name, raw_args)
                if (router_mod.FORGED_IDENTITY_KEYS - {"session_id"}).intersection(raw_args.keys()):
                    raise HarnessError("Model-supplied identity override arguments are forbidden", stage="tool_schema", reason="forged_identity_argument", reportable=False)
                args = {k: v for k, v in raw_args.items() if k not in router_mod.CORRELATION_STRIP_KEYS}
                missing = [r for r in (schema.get("required") or ()) if r not in args]
                if missing:
                    raise HarnessError(f"Missing required argument `{missing[0]}` for {name}", stage="tool_schema", reason="schema_missing_field", reportable=False, details={"field": missing[0]})
                if schema.get("additionalProperties") is False and (set(args) - set(props)):
                    raise HarnessError(f"Undeclared arguments supplied to {name}", stage="tool_schema", reason="schema_unknown_field", reportable=False)
                return args
            _patched_validate._pre015_patched = True
            router_mod.MCPRouter._validate_and_clean_args = _patched_validate
    return installed
'''


def _install_in_process_shim() -> bool:
    ns: dict[str, object] = {}
    exec(_COMPAT_SHIM_CODE, ns)
    return bool(ns["install"]())


_SHIM_ACTIVE = _install_in_process_shim()


def pytest_configure(config):
    config.addinivalue_line("markers", "live_npu: standing A2/A3 integration")


@pytest.fixture(autouse=True)
def isolate_host_environment(monkeypatch, tmp_path, request):
    host = tmp_path / "host"
    home = host / "home"
    directories = {
        "HOME": home, "USERPROFILE": home, "APPDATA": host / "appdata",
        "LOCALAPPDATA": host / "localappdata", "XDG_CONFIG_HOME": host / "xdg-config",
        "XDG_DATA_HOME": host / "xdg-data", "XDG_STATE_HOME": host / "xdg-state",
        "MINDIE_CONFIG_HOME": host / "mindie-config", "MINDIE_DATA_HOME": host / "mindie-data",
        "MINDIE_DIAGNOSTICS_ROOT": host / "diagnostics",
        "CODEX_HOME": host / "codex-home", "PLUGIN_DATA": host / "plugin-data",
        "REMOTE_DEV_AUTH_DIR": host / "remote-auth", "UV_CACHE_DIR": host / "uv-cache",
    }
    for name, path in directories.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(host / "mindie-config" / "mindie-agent" / "codex.json"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(host / "diagnostics-config.json"))
    temporary = tempfile.TemporaryDirectory(prefix="mindie-thin-", ignore_cleanup_errors=True)
    request.addfinalizer(temporary.cleanup)
    monkeypatch.setenv("TMPDIR", temporary.name)
    monkeypatch.setenv("TEMP", temporary.name)
    monkeypatch.setenv("TMP", temporary.name)
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("MINDIE_CODEX_BIN", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    if _SHIM_ACTIVE:
        shim_dir = tmp_path / "pre015-shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        (shim_dir / "sitecustomize.py").write_text(_COMPAT_SHIM_CODE + "\ninstall()\n", encoding="utf-8", newline="\n")
        py_entries = [str(shim_dir), *_HARNESS_PYTHONPATH]
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(py_entries))
    elif _HARNESS_PYTHONPATH:
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(_HARNESS_PYTHONPATH))
    else:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    yield home
