"""[Role] Prepare independently released components and use native idle handoff.
[Invariants] Adapter source and its baseline lock remain immutable. A component
receipt binds the effective lock and installed commits. Plugin updates inherit
the current component floor; bootstrap cannot switch it back to baseline pins.
Expected catalog HTTP/transport misses and JSON rejected at the HTTP boundary
keep shared diagnostic codes; local parsing and unclassified crashes stay internal.
A pure component move reuses the active release's verified publication identity;
installed pins, adapter and runtime checks always rerun locally.
[Timeouts & Locks] External metadata shares 15s across interpreters. The builder
owns 420s and its generation lock; validation takes 180s; idle handoff takes 30s.
After an active switch a 2s bounded storage reclamation runs without the build lock.
[Side-Effect Policy] Builds a private generation, validates without a model, and
publishes through release_activation. No consent or knowledge lease is created.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tomllib
import urllib.error

# Isolated Python omits the script directory; helpers belong to this candidate.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_process import run
from product_contract import identity, package_files, probe, read_json, validate_receipt

from mindie_knowledge.component_bindings import (
    managed_commands,
    required_components,
)

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HOST = frozenset({"FileNotFoundError", "CalledProcessError"})
_HOST_CODES = frozenset({"invalid_harness_version", "invalid_installed_version"})
_TIMEOUT = frozenset({"TimeoutExpired", "TimeoutError"})
_CATALOG_CATEGORIES = frozenset({"temporary_network", "rate_limited", "certificate", "internal"})


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
    category = getattr(exc, "category", None)
    if isinstance(category, str) and category in _CATALOG_CATEGORIES:
        payload["error"]["category"] = category
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
    return data if isinstance(error, dict) else None


def _copy_process(source, target):
    result = getattr(source, "process_result", None)
    if result is not None:
        target.process_result = result
        if getattr(target, "returncode", None) is None:
            target.returncode = result.returncode
    elif (returncode := getattr(source, "returncode", None)) is not None:
        target.returncode = returncode
    return target


def _finite_delay(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def _is_cert_verification_error(exc) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    return isinstance(exc, ssl.SSLError) and getattr(exc, "reason", None) == "CERTIFICATE_VERIFY_FAILED"


def classify_transport_exception(exc):
    """Classify typed stdlib HTTP/TLS/socket transport errors without free-text parsing."""
    if exc is None or isinstance(exc, TimeoutError):
        return None
    if isinstance(exc, urllib.error.HTTPError):
        headers = getattr(exc, "headers", None)
        raw_retry = headers.get("Retry-After") if headers is not None and hasattr(headers, "get") else None
        retry_after = _finite_delay(raw_retry) if raw_retry is not None else None
        return ("rate_limited" if exc.code == 429 else "temporary_network", retry_after)
    if _is_cert_verification_error(exc):
        return ("certificate", None)
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if _is_cert_verification_error(reason) or _is_cert_verification_error(getattr(exc, "__cause__", None)):
            return ("certificate", None)
        return ("temporary_network", None)
    if isinstance(exc, ssl.SSLError):
        if _is_cert_verification_error(getattr(exc, "__cause__", None)):
            return ("certificate", None)
        return ("temporary_network", None)
    if isinstance(
        exc,
        (
            http.client.InvalidURL,
            http.client.ImproperConnectionState,
            http.client.NotConnected,
            http.client.UnimplementedFileMode,
        ),
    ):
        return None
    if isinstance(exc, (ConnectionError, http.client.HTTPException, socket.gaierror, socket.herror)):
        return ("temporary_network", None)
    return None


def _catalog_error(code, *, category="temporary_network", status=None, retry_after=None, source=None):
    error = CatalogError(code)
    error.code = code
    error.category = category
    if type(status) is int and 100 <= status <= 599:
        error.status = status
    if retry_after is not None:
        error.retry_after = retry_after
    return _copy_process(source, error) if source is not None else error


def _catalog_category(code, status, category=None):
    if isinstance(category, str) and category in _CATALOG_CATEGORIES:
        return category
    if code == "release_certificate_error":
        return "certificate"
    if code in {"release_http_error", "release_transport_unavailable"}:
        return "rate_limited" if status == 429 else "temporary_network"
    # A reason code alone cannot prove whether JSON failed remotely or locally.
    return "internal"


def typed_catalog_error(exc):
    """Rebuild typed catalog facts lost at the child or HTTP boundary."""
    try:
        from mindie_knowledge.release_http import ReleaseResponseTooLarge
        from mindie_knowledge.releases_io import ReleaseFetchError
    except ImportError:
        ReleaseResponseTooLarge = ()
        ReleaseFetchError = ()

    envelope = _error_payload(getattr(exc, "mindie_output", None))
    if envelope is None:
        result = getattr(exc, "process_result", None)
        envelope = _error_payload(getattr(result, "stdout", None))
    if envelope is not None:
        payload = envelope["error"]
        kind = payload.get("type")
        code = payload.get("code")
        code = code if isinstance(code, str) and _CODE.fullmatch(code) else None
        if kind in _HOST or code in _HOST_CODES:
            error = FileNotFoundError("component helper is unavailable for catalog selection")
        elif kind in _TIMEOUT:
            error = TimeoutError("component catalog probe timed out")
        elif kind in {"CatalogError", "ReleaseFetchError", "ReleaseJSONError"}:
            status = payload.get("status")
            error = _catalog_error(
                code or "release catalog unavailable",
                category=_catalog_category(code, status, payload.get("category")),
                status=status if type(status) is int else None,
            )
        else:
            from diagnostic_support import reference
            if (diagnostic := reference(envelope)) is not None:
                exc.mindie_diagnostic = diagnostic
            return exc
        return _copy_process(exc, error)
    if isinstance(exc, ReleaseFetchError):
        return _catalog_error(
            exc.code,
            category=_catalog_category(exc.code, exc.status, getattr(exc, "category", None)),
            status=exc.status,
            source=exc,
        )
    if isinstance(exc, urllib.error.HTTPError):
        kind = classify_transport_exception(exc)
        return _catalog_error(
            "release_http_error",
            category=kind[0] if kind else ("rate_limited" if exc.code == 429 else "temporary_network"),
            status=exc.code,
            retry_after=kind[1] if kind else None,
            source=exc,
        )
    if isinstance(exc, ReleaseResponseTooLarge):
        return _catalog_error("release_transport_unavailable", source=exc)
    if (kind := classify_transport_exception(exc)) is not None:
        category, retry_after = kind
        return _catalog_error(
            "release_certificate_error" if category == "certificate" else "release_transport_unavailable",
            category=category,
            retry_after=retry_after,
            source=exc,
        )
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


def _verified_publication(adapter, expected):
    """Reuse the active release's validated publication for pure component moves."""
    release = adapter.get("component_release") if isinstance(adapter, dict) else None
    if not isinstance(release, str) or not release.strip():
        return None
    try:
        value = read_json(Path(release) / "runtime.json")[0]
    except TimeoutError:
        raise
    except (OSError, TypeError, ValueError):
        return None
    validation = value.get("validation") if isinstance(value, dict) else None
    if not isinstance(validation, dict) or validation.get("status") != "validated":
        return None
    fields = ("schema", "source_sha256", "product_sha256", "publication")
    return validation if all(validation.get(field) == expected.get(field)
                             for field in fields) else None


def _lock_baseline_floor(source, baseline_pins):
    """Read official VCS floor metadata for `baseline_pins` from local `uv.lock`."""
    from mindie_knowledge.runtime_inventory import COMPONENTS, validate_targets

    lock_path = Path(source) / "uv.lock"
    if not lock_path.is_file():
        return None
    parsed = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    floor = {}
    for pkg in parsed.get("package", ()):
        name = pkg.get("name")
        if name not in baseline_pins or name not in COMPONENTS:
            continue
        git_url = (pkg.get("source") or {}).get("git", "")
        commit = git_url.rsplit("#", 1)[-1] if "#" in git_url else ""
        repo = COMPONENTS[name]
        if ("github.com/" + repo) not in git_url or commit != baseline_pins[name]:
            return None
        floor[name] = dict(repository=repo, version=str(pkg.get("version", "")), revision=commit)
    if set(floor) != set(baseline_pins):
        return None
    validate_targets(floor)
    return floor


def _inherit_local_floor(source, current_python, baseline, required, adapter, catalog_exc,
                         component_runtime):
    """Fallback to verified local inventories when GitHub release API is unavailable."""
    from semantic_version import Version
    from mindie_knowledge.runtime_inventory import validate_targets

    lock_floor = _lock_baseline_floor(source, baseline["runtime"])
    if lock_floor is None:
        raise catalog_exc
    installed = {
        k: v for k, v in component_runtime.read_inventory(current_python).items()
        if k not in RETIRED_COMPONENTS
    }
    targets = dict(lock_floor)
    candidates = [installed]
    seen_pythons = {str(current_python)}
    for _name, bound_python, _module in managed_commands(adapter or {}).values():
        py_str = str(bound_python)
        if py_str in seen_pythons:
            continue
        seen_pythons.add(py_str)
        try:
            ext_inv = component_runtime.read_inventory(bound_python)
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001 - non-VCS external wheels cannot supply SHA receipts
            continue
        candidates.append({k: v for k, v in ext_inv.items() if k not in RETIRED_COMPONENTS})

    for pool in candidates:
        for name, row in pool.items():
            if name not in targets or Version(row["version"]) > Version(targets[name]["version"]):
                targets[name] = row
            elif Version(row["version"]) == Version(targets[name]["version"]) and row["revision"] != targets[name]["revision"]:
                raise catalog_exc
    validate_targets(targets)
    for name, req_version in (required or {}).items():
        if name not in targets or (
            req_version is not None and Version(targets[name]["version"]) < Version(req_version)
        ):
            raise catalog_exc
    return dict(installed=installed, runtime=targets, changed=targets != installed)


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
    publication = _verified_publication(adapter, baseline)
    required = {
        name: version for name, version in (required_components(adapter) if adapter else {}).items()
        if name not in RETIRED_COMPONENTS
    }
    retired_state = {"removed": False}
    with _without_retired_inventory(component_runtime, retired_state):
        try:
            selection = _discover_catalog(current_python or python, baseline["runtime"], force=force,
                                          required_components=required)
        except CatalogError as exc:
            if current_python is None or force:
                raise
            selection = _inherit_local_floor(
                source, current_python, baseline, required, adapter, exc, component_runtime)
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
    reused = receipt.exists()
    if reused:
        existing = read_json(receipt)[0]
        if os.name == "nt":
            # A cached generation receipt still has to prove this machine's
            # generated pythonw.exe before its entry can be published.
            probe(python, scripts, lambda argv, **kw: run(argv, "", **kw).checked_stdout(),
                  verified_receipt=existing["validation"], components=artifact["components"],
                  require_windows_service=True)
        else:
            validate_receipt(
                json.dumps(existing["validation"]),
                identity(retained, existing["validation"].get("candidate_revision"),
                         components=artifact["components"]),
            )
    else:
        validation = probe(python, scripts, lambda argv, **kw: run(argv, "", **kw).checked_stdout(),
                           verified_receipt=publication,
                           components=artifact["components"],
                           require_windows_service=(os.name == "nt"))
        _atomic(receipt, dict(key=Path(artifact["generation"]).name, python=str(python),
                              source=str(retained), validation=validation))
    publish_entry(python, scripts, release)
    result = dict(python=str(python), scripts=str(scripts), release=str(release))
    if reused:
        result["reused"] = True
    return result


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
    if result.get("status") == "active" and "storage_cleanup" not in result:
        from storage_gc import cleanup_fact
        result["storage_cleanup"] = cleanup_fact(
            data=data, keep_paths=[runtime["python"], adapter.get("python")],
            budget_seconds=2.0)
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
