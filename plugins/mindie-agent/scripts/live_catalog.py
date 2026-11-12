"""Selected-generation catalogs for a resident Codex MCP facade.

[Role] Keep descriptors and component validation paired with the active helper.
[Invariants] Discovery needs no knowledge grant or native task attachment.
Cache keys and descriptors come from the same selected binding.
[Timeouts & Locks] 15s catalog helper; component inner deadline plus 30s grace.
[Side-Effect Policy] Read-only discovery; calls run once under Gate's update lock.
"""
import json
from pathlib import Path
import sys
import threading

from bounded_process import run
from session_gate import runtime_binding, runtime_env, runtime_scripts
from remote_dev.mcp.catalog_refresh import file_revision

HERE = Path(__file__).resolve().parent
_cache = {}
_lock = threading.Lock()


def _binding_revision(value):
    return json.dumps(runtime_binding(value), sort_keys=True), file_revision(
        Path(value.get("runtime_scripts") or HERE) / "mcp_catalog.json")


def revision(config):
    if not Path(config).exists():
        return None
    value = json.loads(Path(config).read_text(encoding="utf-8"))
    return _binding_revision(value)


def _selected(config, *, required=False):
    try:
        value = json.loads(Path(config).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("adapter configuration must be an object")
        if not required and "python" not in value and "runtime_scripts" not in value:
            return None
        if not isinstance(value.get("python"), str) or not Path(value["python"]).is_absolute():
            raise ValueError("selected runtime requires its absolute interpreter path")
        runtime_scripts(value)
        return value
    except TimeoutError as exc:
        exc.request_not_started = True
        exc.request_stage = "runtime_selection"
        raise
    except (OSError, ValueError) as exc:
        exc.request_not_started = True
        exc.request_stage = "runtime_selection"
        raise


def _local(value):
    return (Path(value["python"]).absolute() == Path(sys.executable).absolute()
            and Path(runtime_scripts(value)).resolve() == HERE)


def tools(surface, config, bundled):
    try:
        return _tools(surface, config, bundled)
    except Exception as exc:
        exc.request_not_started = True
        exc.request_stage = "catalog_discovery"
        raise


def _tools(surface, config, bundled):
    if surface == "knowledge" or not Path(config).exists():
        return bundled
    value = _selected(config)
    if value is None or surface != "remote" and _local(value):
        return bundled
    key = _binding_revision(value)
    with _lock:
        if surface not in _cache or _cache[surface][0] != key:
            if surface == "remote":
                catalog = json.loads(
                    (Path(runtime_scripts(value)) / "mcp_catalog.json").read_text(encoding="utf-8"))["remote"]
            else:
                output = run(
                    [value["python"], str(Path(runtime_scripts(value)) / "runtime_call.py")],
                    json.dumps({"surface": "catalog", "catalog_surface": surface}),
                    timeout=15, max_output=2 * 1024 * 1024, env=runtime_env(config, adapter=value),
                )
                catalog = json.loads(output.checked_stdout())
            if not isinstance(catalog, list):
                raise ValueError("selected helper returned an invalid catalog")
            _cache[surface] = key, catalog
        return _cache[surface][1]


def component_call(request, session, config, *, cancel=None, timeout=None):
    value = _selected(config, required=True)
    params = request.get("params") or {}
    if value is None or _local(value):
        from component_tools import execute
        return execute(params.get("name"), params.get("arguments"), session, cancel=cancel, timeout=timeout)
    from mcp_gate import runtime_result
    arguments = params.get("arguments") or {}
    inner = arguments.get("timeout_ms", 120000)
    if type(inner) is not int or not 1 <= inner <= 600000:
        inner = 120000
    output = run(
        [value["python"], str(Path(runtime_scripts(value)) / "runtime_call.py")],
        json.dumps({"surface": "components", "name": params.get("name"), "arguments": arguments,
                    "mindie_session_id": session, "native_session_verified": True,
                    "timeout": timeout}),
        timeout=(inner / 1000 + 30 if timeout is None else timeout + 30),
        max_output=32 * 1024 * 1024, env=runtime_env(config, adapter=value), cancel=cancel,
    )
    return runtime_result(output)
