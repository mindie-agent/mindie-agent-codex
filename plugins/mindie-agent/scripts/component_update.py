"""[Role] Prepare independently released components and use native idle handoff.
[Invariants] Adapter source and its baseline lock remain immutable. A component
receipt binds the effective lock and installed commits. Plugin updates inherit
the current component floor; bootstrap cannot switch it back to baseline pins.
[Timeouts & Locks] External metadata shares 15s across interpreters. The builder
owns 420s and its generation lock; validation takes 180s; idle handoff takes 30s.
[Side-Effect Policy] Builds a private generation, validates without a model, and
publishes through release_activation. No consent or knowledge lease is created.
"""
from __future__ import annotations

import json
import argparse
from pathlib import Path
import sys
import time

# Isolated Python omits the script directory; helpers belong to this candidate.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_process import run
from product_contract import identity, package_files, probe, read_json


COMPONENT_MODULES = {
    "profiler": ("mindie-profiler", "mindie_profiler"),
    "sim": ("mindie-sim", "mindie_sim"),
    "coordinator": ("mindie-coordinator", "mindie_coordinator.cli"),
    "diagnostics": ("mindie-diagnostics", "mindie_diagnostics.cli"),
}


def managed_commands(adapter):
    """Only standard module bindings transfer ownership to the managed runtime."""
    result = {}
    for name, (distribution, module) in COMPONENT_MODULES.items():
        argv = adapter.get("component_commands", {}).get(name)
        if not isinstance(argv, list) or len(argv) < 3 or argv[-2:] != ["-m", module]:
            continue
        flags = argv[1:-2]
        if flags not in ([], ["-I"], ["-I", "-X", "utf8"], ["-I", "-B", "-X", "utf8"]):
            continue
        result[name] = (distribution, argv[0], module)
    return result


def required_components(adapter):
    from mindie_knowledge.component_runtime import read_versions
    deadline = time.monotonic() + 15
    interpreters = {}
    for distribution, python, _module in managed_commands(adapter).values():
        interpreters.setdefault(python, []).append(distribution)
    versions = {}
    for python, names in interpreters.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("bound component metadata budget exhausted")
        versions.update(read_versions(python, names, timeout=remaining))
    return versions


def validate_command_floor(adapter, components):
    from semantic_version import Version
    for name, version in required_components(adapter).items():
        if name not in components or Version(components[name]["version"]) < Version(version):
            raise ValueError(f"candidate runtime is below the current bound component: {name}")


def rebind_commands(adapter, python, components):
    commands = dict(adapter.get("component_commands", {}))
    for name, (distribution, _old_python, module) in managed_commands(adapter).items():
        if distribution not in components:
            raise ValueError(f"candidate runtime omits bound component: {distribution}")
        commands[name] = [str(python), "-I", "-X", "utf8", "-m", module]
    return commands


def prepare(source, python, data, *, current_python=None, force=False, current_source=None,
            adapter=None):
    from mindie_knowledge.component_runtime import build, discover
    from bootstrap import publish_entry, readback
    from release_activation import _atomic

    source, data = Path(source), Path(data)
    baseline = identity(source)
    required = required_components(adapter) if adapter else {}
    selection = discover(current_python or python, baseline["runtime"], force=force,
                         required_components=required)
    needs_binding = any(str(bound_python) != str(current_python or python)
                        for _name, bound_python, _module in managed_commands(adapter or {}).values())
    if not selection["changed"] and current_source == baseline["source_sha256"] and not needs_binding:
        return None
    pins = {name: row["revision"] for name, row in selection["runtime"].items()}
    if pins == baseline["runtime"] and not needs_binding:
        return None
    artifact = build(source, data / "component-runtimes", baseline, selection["runtime"],
                     python=python, adapter_distribution="mindie-agent-codex",
                     files=[str(path.relative_to(source)) for path in package_files(source)])
    python, retained = Path(artifact["python"]), Path(artifact["source"])
    scripts = readback(python, retained)
    release = Path(artifact["generation"]) / "release"
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
    result = json.loads(run(argv, json.dumps(attempt), timeout=180, allow_service=True).checked_stdout())
    return dict(result, runtime=runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("python")
    parser.add_argument("data", type=Path)
    parser.add_argument("current_python")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.python, args.data,
                             current_python=args.current_python,
                             adapter=read_json(args.config)[0] if args.config else None),
                     sort_keys=True))


if __name__ == "__main__":
    main()
