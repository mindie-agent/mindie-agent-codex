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

# Isolated Python omits the script directory; helpers belong to this candidate.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_process import run
from product_contract import identity, package_files, probe, read_json

from mindie_knowledge.component_bindings import (
    managed_commands,
    required_components,
)


def prepare(source, python, data, *, current_python=None, force=False, current_source=None,
            adapter=None):
    from mindie_knowledge.component_runtime import build, discover
    from bootstrap import native_path, publish_entry, readback
    from release_activation import _atomic

    source, data = Path(source), Path(data)
    baseline = identity(source)
    required = required_components(adapter) if adapter else {}
    selection = discover(current_python or python, baseline["runtime"], force=force,
                         required_components=required)
    needs_binding = any(str(bound_python) != str(python)
                        for _name, bound_python, _module in managed_commands(adapter or {}).values())
    if not selection["changed"] and current_source == baseline["source_sha256"] and not needs_binding:
        return None
    pins = {name: row["revision"] for name, row in selection["runtime"].items()}
    if pins == baseline["runtime"] and not needs_binding:
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
    adapter = read_json(args.config)[0] if args.config else None
    if adapter is not None and not adapter.get("component_release") and not managed_commands(adapter):
        print("null")
        return
    print(json.dumps(prepare(args.source, args.python, args.data,
                             current_python=args.current_python,
                             adapter=adapter),
                     sort_keys=True))


if __name__ == "__main__":
    main()
