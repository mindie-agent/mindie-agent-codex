"""Select the prepared native entry for Codex hooks and MCP startup.

[Role]
Resolves the canonical installed release (or first-run bootstrap, or MCP
migration source) and dispatches `runpy` into that entry. `hooks.json` and
`.mcp.json` invoke this module instead of inlining the selector as `python -c`.

[Invariants]
- Hooks never pass `--seed`, so they never migrate; only MCP/startup roles with
  a newer seed may route to the migration bootstrap.
- `stop` never prepares an unconfigured installation.
- Prepared `runtime.json` identity is compared exactly; a mismatch is not a
  first install and does not fall back to plugin cache.

[Timeouts & Locks]
- No locks or subprocesses; the selected entry owns timeouts.

[Side-Effect Policy]
- Importing this module is side-effect free. Running it as `__main__` or via
  `launch()` executes the selected entry.
"""
from __future__ import annotations

import json
import os
import re
import runpy
import sys
from pathlib import Path

MCP_ROLES = ("remote-mcp", "tools-mcp", "knowledge-mcp")
VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!-]{0,79}")
NUMERIC_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")


def read(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("MindIE metadata must be regular")
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("MindIE metadata exceeds its byte bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("MindIE metadata must be an object")
    return value


def version_key(value):
    match = NUMERIC_VERSION.fullmatch(value)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def parse_argv(argv):
    name = "mindie-agent"
    marketplace = "mindie-agent"
    seed = None
    index = 0
    while index < len(argv):
        current = argv[index]
        if current == "--name" and index + 1 < len(argv):
            name = argv[index + 1]
            index += 2
        elif current == "--marketplace" and index + 1 < len(argv):
            marketplace = argv[index + 1]
            index += 2
        elif current == "--seed" and index + 1 < len(argv):
            seed = argv[index + 1]
            index += 2
        elif current == "--":
            return name, marketplace, seed, list(argv[index + 1 :])
        else:
            return name, marketplace, seed, list(argv[index:])
    return name, marketplace, seed, []


def _plugin_source(home, marketplace, name, seed):
    default = home / "plugins/cache" / marketplace / name / (seed or "unprepared")
    return Path(os.environ.get("PLUGIN_ROOT", str(default)))


def _prepared_entry(adapter, data):
    validation = adapter["product_validation"]
    version = validation["native_version"]
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError("MindIE release identity is invalid")
    release = Path(adapter["component_release"]) if adapter.get("component_release") else data / "releases" / version
    receipt = read(release / "runtime.json")
    entry = release / "entry.py"
    if (receipt["python"] != adapter["python"] or receipt["validation"] != validation
            or validation.get("status") != "validated"
            or validation.get("schema") != "mindie-candidate-check/1"):
        raise ValueError("MindIE installed runtime receipt differs")
    return entry


def select_entry(*, name="mindie-agent", marketplace="mindie-agent", seed=None, args=None):
    args = [] if args is None else list(args)
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    data = Path(os.environ.get("PLUGIN_DATA", str(home / "plugins/data" / (name + "-" + marketplace))))
    config = Path(os.environ.get(
        "MINDIE_AGENT_CONFIG", str(Path.home() / ".config/mindie-agent/codex.json"))).expanduser().absolute()
    role = args[0] if args else ""
    if config.exists():
        adapter = read(config)
        migrate = False
        if seed and (role in MCP_ROLES or (role == "bridge" and args[1:2] == ["mcp"])):
            installed = adapter["product_validation"]["native_version"]
            if not isinstance(installed, str) or not VERSION_PATTERN.fullmatch(installed):
                raise ValueError("MindIE release identity is invalid")
            current, target = version_key(installed), version_key(seed)
            migrate = current is not None and target is not None and target > current
        if migrate:
            source = _plugin_source(home, marketplace, name, seed)
            bootstrap = source / "scripts/bootstrap.py"
            if bootstrap.is_file():
                return bootstrap, [str(data), "--source", str(source), "migrate", *args]
            entry = data / "releases" / seed / "entry.py"
            if not entry.is_file():
                raise RuntimeError("MindIE migration source is unavailable")
            return entry, ["migrate", *args]
        return _prepared_entry(adapter, data), args
    fixed = data / "releases" / seed / "entry.py" if seed else None
    if fixed is not None and fixed.is_file():
        return fixed, args
    if args[:1] == ["stop"]:
        raise RuntimeError("MindIE runtime is not prepared; start a new native session")
    source = _plugin_source(home, marketplace, name, seed)
    entry = source / "scripts/bootstrap.py"
    return entry, [str(data), "--source", str(source), *args]


def launch(argv=None):
    name, marketplace, seed, args = parse_argv(sys.argv[1:] if argv is None else argv)
    entry, forwarded = select_entry(name=name, marketplace=marketplace, seed=seed, args=args)
    if not entry.is_file():
        raise RuntimeError("MindIE installed entry is unavailable")
    sys.path.insert(0, str(entry.parent))
    sys.argv = [str(entry), *forwarded]
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    launch()
