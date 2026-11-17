#!/usr/bin/env python3
"""Direct file-based MCP entrypoint for `mindie-agent-codex`.

[Role]
Resolves the durable or cache-resident `hook_launcher.py` without inline JSON
state-machine blobs and forwards CLI arguments (`--seed <ver> remote-mcp|tools-mcp`).

[Invariants]
- Pure Python stdlib; zero imports of `mindie_knowledge` or `torch` at startup.
- Prefers `$PLUGIN_ROOT/scripts/hook_launcher.py`, then `$PLUGIN_DATA/hook_launcher.py`,
  then `$PLUGIN_DATA/releases/<seed>/hook_launcher.py`, then the Codex plugin cache.

[Timeouts & Locks]
- Pure file-existence checks before `runpy.run_path` handoff.

[Side-Effect Policy]
- Strictly read-only locator before delegating to `hook_launcher.py`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys


def resolve_launcher(
    *,
    seed_version: str | None = None,
    name: str = "mindie-agent",
    marketplace: str = "mindie-agent",
) -> Path:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    data = Path(
        os.environ.get(
            "PLUGIN_DATA",
            str(home / "plugins" / "data" / f"{name}-{marketplace}"),
        )
    )
    candidates: list[Path] = []
    root = os.environ.get("PLUGIN_ROOT")
    if root:
        candidates.append(Path(root) / "scripts" / "hook_launcher.py")
    candidates.append(data / "hook_launcher.py")
    if seed_version:
        candidates.append(data / "releases" / seed_version / "hook_launcher.py")
    candidates.append(
        home
        / "plugins"
        / "cache"
        / marketplace
        / name
        / (seed_version or "unprepared")
        / "scripts"
        / "hook_launcher.py"
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    cfg = Path(
        os.environ.get("MINDIE_AGENT_CONFIG", str(Path.home() / ".config/mindie-agent/codex.json"))
    ).expanduser()
    if cfg.is_file():
        with cfg.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) <= 65536:
            parsed = json.loads(raw.decode("utf-8-sig"))
            if isinstance(parsed, dict):
                comp_rel = parsed.get("component_release")
                if isinstance(comp_rel, str) and (Path(comp_rel) / "hook_launcher.py").is_file():
                    return Path(comp_rel) / "hook_launcher.py"
                ver = (parsed.get("product_validation") or {}).get("native_version")
                if isinstance(ver, str) and (data / "releases" / ver / "hook_launcher.py").is_file():
                    return data / "releases" / ver / "hook_launcher.py"
    raise RuntimeError("MindIE launcher is unavailable")


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    seed: str | None = None
    for idx, token in enumerate(args[:-1]):
        if token == "--seed":
            seed = args[idx + 1]
            break
    launcher = resolve_launcher(seed_version=seed)
    sys.argv = [str(launcher), *args]
    runpy.run_path(str(launcher), run_name="__main__")


if __name__ == "__main__":
    main()
