"""[Role] Bounded silent storage reclamation shared by bootstrap, activation and maintenance.
[Invariants]
- Deletion is delegated only to `mindie_knowledge.storage`; this module never
  unlinks runtime generations, releases or user state itself.
- A cleanup failure after a successful primary operation is a
  `cleanup_warning`/`deferred_in_use` fact, never a changed success status.
- `TimeoutError` is re-raised from every broad handler so a budget deadline is
  never misreported as an unclassified local fault.
- The module imports stdlib only at import time so native bootstrap can run it
  before the locked runtime exists.
[Timeouts & Locks]
- New generations run first and each phase receives a bounded slice of the
  caller budget (default 2.0s); no lock is waited on.
- The uv-cache helper performs arithmetic plus `os.stat` only.
[Side-Effect Policy]
- Holds shared runtime leases (their own `.inuse.lock` sidecars) only inside
  existing managed release/runtime directories during a sweep.
- Writes only a same-volume `UV_CACHE_DIR` into a caller-owned environment
  mapping; deletes only disposable paths selected by the shared storage layer.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import re
import sys
import time

DEFAULT_BUDGET_SECONDS = 2.0
DISPATCH_TIMEOUT_SECONDS = 20.0
MIN_PHASE_BUDGET_SECONDS = 0.1
MAX_REPORTED_WARNINGS = 12
UV_CACHE_DIRECTORY = ".uv-cache"


def _existing_ancestor(path):
    probe = Path(path)
    while True:
        try:
            os.stat(probe)
            return probe
        except FileNotFoundError:
            if probe.parent == probe:
                return None
            probe = probe.parent
        except TimeoutError:
            raise
        except OSError:
            return None


def _device(path):
    ancestor = _existing_ancestor(path)
    if ancestor is None:
        return None
    try:
        return os.stat(ancestor).st_dev
    except TimeoutError:
        raise
    except OSError:
        return None


def _default_uv_cache(env):
    xdg = env.get("XDG_CACHE_HOME")
    if isinstance(xdg, str) and xdg.strip():
        return Path(xdg) / "uv"
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "uv" / "cache"
    home = Path.home()
    if sys.platform == "darwin":
        legacy = home / ".cache" / "uv"
        try:
            if legacy.exists() or legacy.is_symlink():
                return legacy
        except TimeoutError:
            raise
        except OSError:
            pass
        return home / "Library" / "Caches" / "uv"
    return home / ".cache" / "uv"


def _cache_devices(cache):
    devices = {_device(cache)}
    try:
        resolved = Path(cache).resolve()
    except TimeoutError:
        raise
    except OSError:
        resolved = None
    if resolved is not None:
        devices.add(_device(resolved))
    return devices


def configure_same_volume_uv_cache(target_dir, env, *, preferred=None):
    """Pin uv's cache to the target device when its default cache crosses one.

    A caller-supplied non-empty ``UV_CACHE_DIR`` always wins. This is the
    stdlib-equivalent of the shared storage helper for native bootstrap, where
    `mindie_knowledge` is not importable yet.
    """
    if not isinstance(env, dict):
        return env
    explicit = env.get("UV_CACHE_DIR")
    if isinstance(explicit, str) and explicit.strip():
        return env
    target = Path(target_dir)
    target_device = _device(target)
    if target_device is None:
        return env
    cache = _default_uv_cache(env)
    if target_device in _cache_devices(cache):
        return env
    try:
        resolved = target.resolve()
    except TimeoutError:
        raise
    except OSError:
        resolved = target.absolute()
    candidates = [preferred] if preferred is not None else []
    candidates.extend((resolved / UV_CACHE_DIRECTORY,
                       resolved.parent / UV_CACHE_DIRECTORY))
    for candidate in candidates:
        if _device(candidate) == target_device:
            env["UV_CACHE_DIR"] = str(candidate)
            return env
    return env


def configure_bootstrap_uv_cache(data, runtime, env):
    """Prefer `<data>/.uv-cache` when uv's default cache is on another volume."""
    return configure_same_volume_uv_cache(
        runtime, env, preferred=Path(data) / UV_CACHE_DIRECTORY)


def _plugin_data(data):
    if data is not None:
        return Path(data).expanduser().absolute()
    configured = os.environ.get("PLUGIN_DATA")
    if configured:
        return Path(configured).expanduser().absolute()
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return (home / "plugins/data/mindie-agent-mindie-agent").absolute()


def _config_home(value):
    return Path(value or os.environ.get(
        "MINDIE_CONFIG_HOME", Path.home() / ".config/mindie-agent")).expanduser().absolute()


def _data_home(value):
    return Path(value or os.environ.get(
        "MINDIE_DATA_HOME", Path.home() / ".local/share/mindie-agent")).expanduser().absolute()


def _infer_codex_home(plugin_data):
    """Recover `<codex_home>` from the default `<codex>/plugins/data/<name>` shape."""
    plugin_data = Path(plugin_data)
    if plugin_data.parent.name == "data" and plugin_data.parent.parent.name == "plugins":
        return plugin_data.parent.parent.parent
    return None


def _codex_home(value, plugin_data=None):
    if value is not None:
        return Path(value).expanduser().absolute()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().absolute()
    inferred = _infer_codex_home(_plugin_data(plugin_data))
    if inferred is not None:
        return inferred
    return (Path.home() / ".codex").absolute()


def _warning(value, label, error):
    value.setdefault("warnings", []).append(label + ":" + type(error).__name__)


_RELEASE_PREFIX = re.compile(r"^[0-9]+(?:\.[0-9]+)+")


def _version_key(name):
    name = name[1:] if name[:1] in "vV" else name
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"[.+-]", name))


def _strip_win_prefix(text):
    if text[:8].upper() == "\\\\?\\UNC\\":
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\") or text.startswith("//?/"):
        return text[4:]
    return text


def _normalized(value):
    raw = _strip_win_prefix(os.fspath(value).strip())
    return Path(os.path.normpath(os.path.abspath(os.path.expanduser(raw))))


def _release_dir(value, releases_root):
    """Return ``<releases_root>/<version>`` when *value* names that release."""
    try:
        text = os.fspath(value).strip().strip('"').strip("'")
    except (TypeError, ValueError):
        return None
    if not text:
        return None
    try:
        candidate, root = _normalized(text), _normalized(releases_root)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    try:
        common = os.path.commonpath((str(candidate), str(root)))
    except ValueError:
        return None
    if os.path.normcase(common) != os.path.normcase(str(root)):
        return None
    first = os.path.relpath(str(candidate), str(root)).split(os.sep)[0]
    if first in {".", ".."} or not _RELEASE_PREFIX.match(first):
        return None
    return root / first


def _strings(value, depth=0):
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, depth + 1)


def _read_object(path):
    try:
        if not Path(path).is_file():
            return None
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _release_dirs(releases_root):
    try:
        with os.scandir(releases_root) as iterator:
            entries = [Path(item.path) for item in iterator
                       if not item.is_symlink() and item.is_dir()
                       and _RELEASE_PREFIX.match(item.name)]
    except TimeoutError:
        raise
    except OSError:
        return []
    return sorted(entries, key=lambda item: _version_key(item.name))


def _collect_protected_state(plugin_data, config_home, codex_home, keep_paths,
                             *, include_default_config=True):
    """Every release/runtime that a host binding may still execute.

    ``sweep_legacy_installations`` keeps leased directories but only reads
    top-level ``native_version``/``version``. Codex stores the version under
    ``metadata.version`` and may bind ``python`` while ``component_release`` is
    ``None``. Collect those live pins here, then hold a shared runtime lease on
    every existing directory across the destructive legacy phase. The returned
    set is also forwarded as an explicit keep list to the component sweep.
    """
    from mindie_knowledge.storage import owning_dir

    plugin_data = Path(plugin_data)
    releases_root = plugin_data / "releases"
    runtimes_root = plugin_data / "runtimes"
    protected = {}

    def add(value):
        if value is None:
            return
        try:
            path = _normalized(value)
        except TimeoutError:
            raise
        except (OSError, TypeError, ValueError):
            return
        protected.setdefault(str(path), path)

    def add_owned(value):
        add(value)
        if not isinstance(value, str) or not value.strip():
            return
        try:
            owner = owning_dir(str(_normalized(value)))
        except TimeoutError:
            raise
        except (OSError, RuntimeError, ValueError):
            owner = None
        if owner is not None:
            add(owner)

    candidates = []
    configured = os.environ.get("MINDIE_AGENT_CONFIG")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path(config_home) / "codex.json")
    if include_default_config:
        candidates.append(Path.home() / ".config/mindie-agent/codex.json")

    adapter_pythons, adapter_owners, adapter_versions = set(), set(), set()
    seen_documents = set()
    for path in candidates:
        try:
            key = os.path.normcase(os.path.abspath(str(path)))
        except TimeoutError:
            raise
        except (OSError, TypeError, ValueError):
            continue
        if key in seen_documents:
            continue
        seen_documents.add(key)
        adapter = _read_object(path)
        if adapter is None:
            continue
        release = adapter.get("component_release")
        if isinstance(release, str) and release.strip():
            add_owned(release)
            release_dir = _release_dir(release, releases_root)
            if release_dir is not None:
                add(release_dir)
                adapter_versions.add(release_dir.name)
        python = adapter.get("python")
        if isinstance(python, str) and python.strip():
            add_owned(python)
            try:
                normalized_python = _normalized(python)
                adapter_pythons.add(os.path.normcase(str(normalized_python)))
                adapter_pythons.add(os.path.normcase(str(_normalized(os.path.realpath(normalized_python)))))
            except TimeoutError:
                raise
            except (OSError, ValueError):
                pass
            try:
                owner = owning_dir(str(_normalized(python)))
            except TimeoutError:
                raise
            except (OSError, RuntimeError, ValueError):
                owner = None
            if owner is not None:
                adapter_owners.add(os.path.normcase(str(_normalized(owner))))
        add_owned(adapter.get("runtime_scripts"))
        for text in _strings(adapter.get("domain_configs")):
            release_dir = _release_dir(text, releases_root)
            if release_dir is not None:
                add(release_dir)
                adapter_versions.add(release_dir.name)
        for text in _strings(adapter.get("component_commands")):
            release_dir = _release_dir(text, releases_root)
            if release_dir is not None:
                add(release_dir)
                adapter_versions.add(release_dir.name)

    for item in keep_paths:
        if not item:
            continue
        release_dir = _release_dir(item, releases_root)
        if release_dir is not None:
            add(release_dir)
        try:
            if _normalized(item).is_dir():
                add(item)
        except TimeoutError:
            raise
        except (OSError, TypeError, ValueError):
            pass
        add_owned(str(item))

    release_rows = [(_version_key(item.name), item.name, item)
                    for item in _release_dirs(releases_root)]
    active_versions = set(adapter_versions)
    for _key, name, path in release_rows:
        receipt = _read_object(path / "runtime.json")
        if receipt is None:
            continue
        receipt_python = receipt.get("python")
        if isinstance(receipt_python, str) and receipt_python.strip():
            try:
                normalized_python = _normalized(receipt_python)
                matches = (
                    os.path.normcase(str(normalized_python)) in adapter_pythons
                    or os.path.normcase(str(_normalized(os.path.realpath(normalized_python)))) in adapter_pythons)
            except TimeoutError:
                raise
            except (OSError, ValueError):
                normalized_python, matches = None, False
            if not matches and normalized_python is not None:
                try:
                    owner = owning_dir(str(normalized_python))
                except TimeoutError:
                    raise
                except (OSError, RuntimeError, ValueError):
                    owner = None
                matches = owner is not None and os.path.normcase(str(_normalized(owner))) in adapter_owners
            if matches:
                active_versions.add(name)
                add(path)

    native = [plugin_data / "codex.updates/native.json",
              Path(codex_home) / "codex.updates/native.json",
              Path(config_home) / "codex.updates/native.json"]
    protected_versions = set()
    native_active_versions = set()
    seen_native = set()
    for path in native:
        try:
            key = os.path.normcase(os.path.abspath(str(path)))
        except TimeoutError:
            raise
        except (OSError, TypeError, ValueError):
            continue
        if key in seen_native:
            continue
        seen_native.add(key)
        value = _read_object(path)
        if value is None or value.get("stage") == "superseded":
            continue
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        preparation = (value.get("preparation")
                       if isinstance(value.get("preparation"), dict) else {})
        version = metadata.get("version")
        if value.get("stage") != "active":
            if isinstance(version, str) and version.strip():
                protected_versions.add(version)
            add_owned(preparation.get("release"))
            if (release_dir := _release_dir(preparation.get("release"),
                                            releases_root)) is not None:
                add(release_dir)
            add_owned(preparation.get("python"))
        for candidate in (version, value.get("native_version"), value.get("version")):
            if isinstance(candidate, str) and candidate.strip():
                native_active_versions.add(candidate)
    if not active_versions:
        active_versions.update(native_active_versions)

    pending_keys = {_version_key(value) for value in protected_versions}
    for key, _name, path in release_rows:
        if key in pending_keys:
            add(path)
    if active_versions:
        threshold = min(_version_key(value) for value in active_versions)
        older = []
        for key, _name, path in release_rows:
            if key >= threshold:
                add(path)
            else:
                older.append((key, path))
        if older:
            add(max(older, key=lambda item: item[0])[1])
    else:
        for _key, _name, path in release_rows[-2:]:
            add(path)

    # A protected release owns the exact runtime named by its immutable receipt.
    for path in list(protected.values()):
        if path.parent != releases_root or not path.is_dir():
            continue
        receipt = _read_object(path / "runtime.json")
        if receipt is None:
            continue
        add_owned(receipt.get("python"))
        key = receipt.get("key")
        if isinstance(key, str) and key.strip():
            clean_key = key.strip()
            add(runtimes_root / clean_key)
            if len(clean_key) > 24:
                add(runtimes_root / clean_key[:24])
    return set(protected.values())


# The task contract spells this helper with a capital S.
_collect_protected_State = _collect_protected_state


def _inside(path, root):
    try:
        lhs, rhs = str(_normalized(path)), str(_normalized(root))
        common = os.path.commonpath((lhs, rhs))
    except (OSError, TypeError, ValueError):
        return False
    return os.path.normcase(common) == os.path.normcase(rhs)


def _hold_protected_leases(stack, protected, plugin_data, data_home=None):
    """Lease every existing protected release/runtime for the destructive phase."""
    from mindie_knowledge.storage import acquire_runtime_lease

    roots = [Path(plugin_data) / name for name in
             ("releases", "runtimes", "component-runtimes", "update/component-runtimes")]
    if data_home is not None:
        roots.extend(Path(data_home) / name
                     for name in ("releases", "runtimes", "component-runtimes"))
    failures = []
    for directory in sorted({Path(item) for item in protected},
                            key=lambda item: str(item)):
        try:
            normalized_dir = _normalized(directory)
            if not normalized_dir.is_dir() or not any(_inside(normalized_dir, root) for root in roots):
                continue
            stack.enter_context(acquire_runtime_lease(normalized_dir))
        except TimeoutError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(exc)
    return failures


def _legacy_phase(config_home, data_home, codex_home, budget, force, result):
    from mindie_knowledge.storage import sweep_legacy_installations

    result["legacy"] = sweep_legacy_installations(
        config_home=config_home, data_home=data_home, codex_home=codex_home,
        budget_seconds=budget, force=force)


def _component_phase(component_root, keep_paths, budget, include_global_roots, result):
    from mindie_knowledge.storage import prune_component_runtimes

    if not Path(component_root).is_dir():
        result["components"] = {"status": "absent", "root": str(component_root),
                                "reclaimed_dirs": 0, "deferred_dirs": 0,
                                "kept_dirs": 0, "warnings": [], "partial": False}
        return
    result["components"] = prune_component_runtimes(
        Path(component_root), keep_paths=keep_paths, keep_recent_verified=2,
        budget_seconds=budget, include_global_roots=include_global_roots)


def _merge(result, phase):
    if not isinstance(phase, dict):
        return
    result["reclaimed_dirs"] += int(phase.get("reclaimed_dirs") or 0)
    result["deferred_dirs"] += int(phase.get("deferred_dirs") or 0)
    result["kept_dirs"] += int(phase.get("kept_dirs") or 0)
    result["reclaimed_files"] += int(phase.get("reclaimed_files") or 0)
    result["reclaimed_trash"] += int(phase.get("reclaimed_trash") or 0)
    for warning in list(phase.get("warnings") or ())[:MAX_REPORTED_WARNINGS]:
        if isinstance(warning, str):
            result["warnings"].append(warning[:200])
    if phase.get("partial"):
        result["partial"] = True


def reclaim_storage(*, data=None, config_home=None, data_home=None, codex_home=None,
                    keep_paths=(), budget_seconds=DEFAULT_BUDGET_SECONDS, force=False,
                    include_global_roots=True):
    """Run both bounded sweeps and return an honest cleanup receipt.

    A storage budget exhaustion or an unavailable shared storage layer becomes
    a deferred/cleanup_warning fact. Other primary-operation state is never
    touched, and the caller keeps its already successful business status.
    """
    explicit_config = (config_home is not None
                       or bool(os.environ.get("MINDIE_CONFIG_HOME")))
    config_home, data_home = _config_home(config_home), _data_home(data_home)
    plugin_data = _plugin_data(data)
    codex_home = _codex_home(codex_home, plugin_data)
    keep = [str(Path(item)) for item in keep_paths if item]
    started = time.monotonic()
    budget = max(0.0, float(budget_seconds))
    deadline = started + budget
    result = dict(schema="mindie-codex-storage-gc/1", status="completed",
                  reclaimed_dirs=0, deferred_dirs=0, kept_dirs=0,
                  reclaimed_files=0, reclaimed_trash=0, warnings=[],
                  partial=False, data=str(plugin_data),
                  config_home=str(config_home), data_home=str(data_home),
                  codex_home=str(codex_home), elapsed_seconds=0.0)
    try:
        protected_ok = True
        try:
            protected = _collect_protected_state(
                plugin_data, config_home, codex_home, keep,
                include_default_config=not explicit_config)
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - cleanup is never a primary failure
            protected, protected_ok = set(), False
            result["partial"] = True
            _warning(result, "protected", exc)
        expanded = keep + [str(item) for item in sorted(protected, key=str)]
        with ExitStack() as leases:
            lease_failures = _hold_protected_leases(leases, protected, plugin_data, data_home)
            if lease_failures:
                protected_ok = False
                result["partial"] = True
                _warning(result, "lease", lease_failures[0])
            if protected_ok:
                try:
                    remaining = max(MIN_PHASE_BUDGET_SECONDS, deadline - time.monotonic())
                    legacy_budget = min(remaining, max(MIN_PHASE_BUDGET_SECONDS, budget * 0.6))
                    _legacy_phase(config_home, data_home, codex_home, legacy_budget, force, result)
                except TimeoutError:
                    raise
                except ImportError as exc:
                    result["status"] = "unavailable"
                    _warning(result, "legacy", exc)
                except OSError as exc:
                    _warning(result, "legacy", exc)
                except Exception as exc:  # noqa: BLE001 - cleanup is never a primary failure
                    _warning(result, "legacy", exc)
                _merge(result, result.get("legacy"))
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("storage reclamation budget exhausted")
                component_budget = max(MIN_PHASE_BUDGET_SECONDS, min(remaining, budget * 0.4))
                _component_phase(plugin_data / "component-runtimes", expanded,
                                 component_budget, include_global_roots, result)
            except TimeoutError:
                raise
            except ImportError as exc:
                result["status"] = "unavailable"
                _warning(result, "components", exc)
            except OSError as exc:
                _warning(result, "components", exc)
            except Exception as exc:  # noqa: BLE001 - cleanup is never a primary failure
                _warning(result, "components", exc)
            _merge(result, result.get("components"))
    except TimeoutError:
        result["status"] = "deferred"
        result["partial"] = True
        result["warnings"].append("budget exhausted")
    if result["deferred_dirs"]:
        result["status"] = "deferred_in_use"
    elif result["partial"]:
        result["status"] = "partial"
    if result["warnings"]:
        result["status"] = "partial" if result["status"] == "completed" else result["status"]
        result["cleanup_warning"] = list(result["warnings"][:MAX_REPORTED_WARNINGS])
    result["deferred_in_use"] = result["deferred_dirs"]
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def cleanup_fact(**options):
    """Run one cleanup as a fact: a successful caller never fails on GC.

    The inner handler re-raises `TimeoutError` before any broad classification;
    the outer handler then records the honest deferred fact instead of leaking
    a cleanup deadline into the already successful primary operation.
    """
    try:
        try:
            return reclaim_storage(**options)
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - cleanup never changes success
            return {"status": "cleanup_warning", "error_type": type(exc).__name__,
                    "warnings": ["reclaim:" + type(exc).__name__],
                    "deferred_in_use": 0}
    except TimeoutError:
        return {"status": "deferred", "reason": "budget_exhausted",
                "warnings": ["reclaim:TimeoutError"], "deferred_in_use": 0}


def dispatch_cleanup(python, *, data=None, keep_paths=(), budget_seconds=1.5,
                     timeout=DISPATCH_TIMEOUT_SECONDS, config_home=None,
                     data_home=None, codex_home=None, force=False):
    """Run the storage worker in a healthy runtime without blocking the caller.

    Native bootstrap is stdlib-only, so a knowledge-enabled interpreter runs
    this same module as a child. Any failure, including argv construction, is
    an honest cleanup warning and never changes an already successful caller.
    """
    try:
        try:
            if not python:
                return {"status": "unavailable",
                        "warnings": ["no healthy interpreter"]}
            script = Path(__file__).resolve()
            argv = [str(python), "-I", "-B", str(script),
                    "--data", str(_plugin_data(data)),
                    "--budget", str(max(0.1, float(budget_seconds)))]
            for option, value in (("--config-home", config_home),
                                  ("--data-home", data_home),
                                  ("--codex-home", codex_home)):
                if value is not None:
                    argv.extend((option, str(value)))
            for keep in keep_paths:
                if keep:
                    argv.extend(("--keep", str(keep)))
            if force:
                argv.append("--force")
            env = {key: value for key, value in os.environ.items()
                   if key != "PYTHONPATH"}
            from bounded_process import run
            output = run(argv, "", timeout=timeout, env=env,
                         max_output=65536).checked_stdout()
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - cleanup never changes the primary result
            return {"status": "cleanup_warning", "error_type": type(exc).__name__,
                    "warnings": ["dispatch:" + type(exc).__name__]}
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError("storage cleanup response is not an object")
        return value
    except TimeoutError:
        return {"status": "deferred", "reason": "budget_exhausted",
                "warnings": ["dispatch:TimeoutError"]}
    except Exception as exc:  # noqa: BLE001 - cleanup never changes the primary result
        return {"status": "cleanup_warning", "error_type": type(exc).__name__,
                "warnings": ["dispatch:" + type(exc).__name__]}


@contextmanager
def serving_runtime_lease(python=None):
    """Hold a shared lease on the managed generation serving a resident MCP.

    Lease acquisition is best-effort: an already-running pre-lease process is
    still protected by command-line image inspection, so a missing sidecar
    must never block an otherwise healthy MCP session.
    """
    try:
        from mindie_knowledge.storage import acquire_runtime_lease, owning_dir
    except ImportError:
        yield
        return
    try:
        owner = owning_dir(str(python or sys.executable))
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        owner = None
    if owner is None:
        yield
        return
    try:
        lease = acquire_runtime_lease(owner)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        yield
        return
    with lease:
        yield lease


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--config-home", type=Path)
    parser.add_argument("--data-home", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--keep", action="append", default=[])
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(reclaim_storage(
        data=args.data, config_home=args.config_home, data_home=args.data_home,
        codex_home=args.codex_home, keep_paths=args.keep,
        budget_seconds=args.budget, force=args.force), sort_keys=True))


if __name__ == "__main__":
    main()
