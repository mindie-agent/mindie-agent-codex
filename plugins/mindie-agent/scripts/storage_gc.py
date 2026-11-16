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
- Writes only a same-volume `UV_CACHE_DIR` into a caller-owned environment
  mapping; deletes only disposable paths selected by the shared storage layer.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
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
            _component_phase(plugin_data / "component-runtimes", keep,
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
