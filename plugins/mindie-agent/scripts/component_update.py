"""[Role] Prepare independently released components and use native idle handoff.
[Invariants] Adapter source and its baseline lock remain immutable. A component
receipt binds the effective lock and installed commits. Plugin updates inherit
the current component floor; bootstrap cannot switch it back to baseline pins.
Expected catalog HTTP/transport/malformed-feed misses keep shared diagnostic
codes; unclassified helper crashes stay internal.
[Timeouts & Locks] External metadata shares 15s across interpreters. The builder
owns 420s and its generation lock; validation takes 180s; idle handoff takes 30s.
[Side-Effect Policy] Builds a private generation, validates without a model, and
publishes through release_activation. No consent or knowledge lease is created.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sys
import urllib.error

# Isolated Python omits the script directory; helpers belong to this candidate.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_process import run
from product_contract import identity, package_files, probe, read_json

from mindie_knowledge.component_bindings import (
    managed_commands,
    required_components,
)

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HOST = frozenset({"FileNotFoundError", "CalledProcessError"})
_HOST_CODES = frozenset({"invalid_harness_version", "invalid_installed_version"})
_TIMEOUT = frozenset({"TimeoutExpired", "TimeoutError"})


class CatalogError(RuntimeError):
    """Expected catalog miss; static codes only, never raw feed text."""


def emit_catalog_error(exc) -> None:
    """Print one static catalog error object; do not echo feed or host text."""
    payload = {"error": {"type": type(exc).__name__}}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _CODE.fullmatch(code):
        payload["error"]["code"] = code
    status = getattr(exc, "status", None)
    if type(status) is int and 100 <= status <= 599:
        payload["error"]["status"] = status
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(1)


def _error_payload(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw.strip())
    except (ValueError, TypeError):
        return None
    error = data.get("error") if isinstance(data, dict) else None
    return error if isinstance(error, dict) else None


def _copy_process(source, target):
    result = getattr(source, "process_result", None)
    if result is not None:
        target.process_result = result
        if getattr(target, "returncode", None) is None:
            target.returncode = result.returncode
    elif (returncode := getattr(source, "returncode", None)) is not None:
        target.returncode = returncode
    return target


def _catalog_error(code, *, category="temporary_network", status=None, source=None):
    error = CatalogError(code)
    error.code = code
    error.category = category
    if type(status) is int and 100 <= status <= 599:
        error.status = status
    return _copy_process(source, error) if source is not None else error


def typed_catalog_error(exc):
    """Rebuild typed catalog facts lost at the child or HTTP boundary."""
    from mindie_knowledge.release_http import ReleaseResponseTooLarge
    from mindie_knowledge.releases_io import ReleaseFetchError

    payload = _error_payload(getattr(exc, "mindie_output", None))
    if payload is None:
        result = getattr(exc, "process_result", None)
        payload = _error_payload(getattr(result, "stdout", None))
    if payload is not None:
        kind = payload.get("type")
        code = payload.get("code")
        code = code if isinstance(code, str) and _CODE.fullmatch(code) else None
        if kind in _HOST or code in _HOST_CODES:
            error = FileNotFoundError("component helper is unavailable for catalog selection")
        elif kind in _TIMEOUT:
            error = TimeoutError("component catalog probe timed out")
        else:
            status = payload.get("status")
            error = _catalog_error(
                code or "release catalog unavailable",
                category="rate_limited" if status == 429 else "temporary_network",
                status=status if type(status) is int else None,
            )
        return _copy_process(exc, error)
    if isinstance(exc, ReleaseFetchError):
        return _catalog_error(
            exc.code,
            category="rate_limited" if exc.status == 429 else "temporary_network",
            status=exc.status,
            source=exc,
        )
    if isinstance(exc, urllib.error.HTTPError):
        return _catalog_error(
            "release_http_error",
            category="rate_limited" if exc.code == 429 else "temporary_network",
            status=exc.code,
            source=exc,
        )
    if isinstance(exc, json.JSONDecodeError):
        return _catalog_error("release_catalog_invalid", source=exc)
    if isinstance(exc, (urllib.error.URLError, ConnectionError, ReleaseResponseTooLarge)):
        return _catalog_error("release_transport_unavailable", source=exc)
    if isinstance(exc, (FileNotFoundError, TimeoutError)):
        return exc
    return exc


def _discover_catalog(*args, **kwargs):
    from mindie_knowledge.component_runtime import discover

    try:
        return discover(*args, **kwargs)
    except TimeoutError:
        raise
    except Exception as exc:
        raise typed_catalog_error(exc) from None


# Retired runtime packages must never be inherited from an older installation
# or queried against the release catalog during component upgrades.
RETIRED_COMPONENTS = frozenset({"mindie-bench"})


@contextmanager
def _without_retired_inventory(runtime_module, retired_state: dict):
    original = getattr(runtime_module, "read_inventory", None)
    if original is None:
        yield
        return

    def filtered_inventory(python, *args, **kwargs):
        installed = dict(original(python, *args, **kwargs))
        removed = {name: installed.pop(name) for name in list(installed) if name in RETIRED_COMPONENTS}
        if removed:
            retired_state["removed"] = True
        return installed

    runtime_module.read_inventory = filtered_inventory
    try:
        yield
    finally:
        runtime_module.read_inventory = original


def prepare(source, python, data, *, current_python=None, force=False, current_source=None,
            adapter=None):
    import mindie_knowledge.component_runtime as component_runtime
    from mindie_knowledge.component_runtime import build
    from bootstrap import native_path, publish_entry, readback
    from release_activation import _atomic

    # uv derives file URIs from these logical paths. Extend the completed
    # artifact paths below, where Python imports and profile I/O need them.
    source, data = Path(source), Path(data)
    baseline = identity(source)
    required = {
        name: version for name, version in (required_components(adapter) if adapter else {}).items()
        if name not in RETIRED_COMPONENTS
    }
    retired_state = {"removed": False}
    with _without_retired_inventory(component_runtime, retired_state):
        selection = _discover_catalog(current_python or python, baseline["runtime"], force=force,
                                      required_components=required)
    if isinstance(selection.get("runtime"), dict):
        for name in RETIRED_COMPONENTS:
            if name in selection["runtime"]:
                selection["runtime"] = {
                    k: v for k, v in selection["runtime"].items() if k not in RETIRED_COMPONENTS
                }
                retired_state["removed"] = True
                break
    needs_binding = any(str(bound_python) != str(python)
                        for _name, bound_python, _module in managed_commands(adapter or {}).values())
    if (not selection["changed"] and not retired_state["removed"]
            and current_source == baseline["source_sha256"] and not needs_binding):
        return None
    pins = {name: row["revision"] for name, row in selection["runtime"].items()}
    if (current_python is not None or not selection["changed"]) and pins == baseline["runtime"] and not needs_binding:
        return None
    artifact = build(source, data / "component-runtimes", baseline, selection["runtime"],
                     python=python, adapter_distribution="mindie-agent-codex",
                     files=[str(path.relative_to(source)) for path in package_files(source)])
    python, retained = native_path(artifact["python"]), native_path(artifact["source"])
    scripts = readback(python, retained)
    release = native_path(artifact["generation"]) / "release"
    release.mkdir(exist_ok=True)
    receipt = release / "runtime.json"
    if not receipt.exists():
        validation = probe(python, scripts, lambda argv, **kw: run(argv, "", **kw).checked_stdout(),
                           components=artifact["components"])
        _atomic(receipt, dict(key=Path(artifact["generation"]).name, python=str(python),
                              source=str(retained), validation=validation))
    publish_entry(python, scripts, release)
    return dict(python=str(python), scripts=str(scripts), release=str(release))


def check(config, source, data, *, force=False):
    from release_activation import ATTEMPT_SCHEMA
    from session_gate import runtime_binding
    adapter = read_json(config)[0]
    attempt = dict(schema=ATTEMPT_SCHEMA, expected_binding=runtime_binding(adapter))
    runtime = prepare(source, adapter["python"], data, force=force,
                      current_source=adapter["product_validation"]["source_sha256"],
                      adapter=adapter)
    if runtime is None:
        return dict(status="up_to_date")
    argv = [runtime["python"], "-I", str(Path(runtime["scripts"]) / "release_activation.py"),
            str(config), runtime["release"], "--attempt"]
    try:
        output = run(argv, json.dumps(attempt), timeout=180, allow_service=True).checked_stdout()
    except TimeoutError:
        raise
    except Exception as exc:
        raise typed_catalog_error(exc) from None
    result = json.loads(output)
    return dict(result, runtime=runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("python")
    parser.add_argument("data", type=Path)
    parser.add_argument("current_python")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    adapter = read_json(args.config)[0] if args.config else None
    try:
        if adapter is not None and not adapter.get("component_release") and not managed_commands(adapter):
            print("null")
            return
        print(json.dumps(prepare(args.source, args.python, args.data,
                                 current_python=args.current_python,
                                 adapter=adapter),
                         sort_keys=True))
    except TimeoutError:
        raise
    except Exception as exc:
        typed = typed_catalog_error(exc)
        if typed is not exc or isinstance(typed, (CatalogError, FileNotFoundError)):
            emit_catalog_error(typed)
        raise


if __name__ == "__main__":
    main()
