"""Validate the release's declared Harness interfaces without running a model.

[Role]
Validates `harness-compatibility.json` (`validate_compatibility`), verifies that all registered `code_paths` and `test_ids` exist in the repository via AST inspection, checks `require_harness(version)`, and builds `release-manifest.json`.

[Invariants]
- Every `used_interfaces` entry in `harness-compatibility.json` must cover the overall `supported` version ranges and reference existing AST symbols and test methods.

[Timeouts & Locks]
- Purely synchronous AST and JSON validation; no locks or subprocesses.

[Side-Effect Policy]
- Read-only validation except when `--output` is passed to write `release-manifest.json`.
"""
import argparse
import ast
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from product_contract import native_version, package_root, read_json

REPOSITORY = "mindie-agent/mindie-agent-codex"


def compatibility(source):
    from mindie_knowledge.releases import validate_compatibility
    return validate_compatibility(
        read_json(package_root(source) / "harness-compatibility.json")[0],
        harness_name="codex",
    )


def metadata(source, revision):
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("release source requires a full immutable commit")
    return dict(
        schema="mindie-plugin-release/1", version=native_version(source),
        source=dict(repository=REPOSITORY, sha=revision),
        compatibility=compatibility(source),
    )


def require_harness(source):
    """Check the actual selected executable once before first profile writes."""
    from native_context import capture
    from harness_update import harness_version
    from mindie_knowledge.releases import supports_harness
    context = capture()
    native = context.get("MINDIE_CODEX_BIN")
    if native is None:
        raise ValueError("selected Codex executable is unavailable")
    import os
    version = harness_version(native, dict(os.environ, **context))
    if not supports_harness(compatibility(source), harness_name="codex", harness_version=version):
        raise ValueError("this Release does not support the selected Codex version")
    return version


def _has_symbol(path, symbol):
    nodes = ast.parse(path.read_text(encoding="utf-8")).body
    for segment in symbol.split("."):
        selected = next((node for node in nodes if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and node.name == segment), None)
        if selected is None:
            return False
        nodes = selected.body
    return True


def check_registry(source, tests):
    source, tests = package_root(source).resolve(), Path(tests).resolve()
    value = compatibility(source)
    for interface in value["used_interfaces"]:
        row = value["interface_registry"][interface]
        for reference in row["code_paths"]:
            filename, symbol = reference.split(":", 1)
            path = (source / filename).resolve()
            if not path.is_relative_to(source) or not _has_symbol(path, symbol):
                raise ValueError("registered Harness code boundary is missing: " + interface)
        for reference in row["test_ids"]:
            filename, symbol = reference.split("::", 1)
            path = (tests / Path(filename).relative_to("tests")).resolve()
            if not path.is_relative_to(tests) or not _has_symbol(path, symbol):
                raise ValueError("registered Harness contract test is missing: " + interface)
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--tests", type=Path)
    args = parser.parse_args()
    if args.tests is not None:
        check_registry(args.source, args.tests)
    print(json.dumps(metadata(args.source, args.revision), sort_keys=True))
