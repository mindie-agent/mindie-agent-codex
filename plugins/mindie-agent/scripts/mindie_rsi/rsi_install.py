#!/usr/bin/env python3
"""Install the host-side standard RSI entrypoint and binding template.

[Role] Makes the RSI action path stable during customer installation.
[Invariants] Existing entrypoints and binding files are preserved; generated
             files contain no customer shell command.
[Timeouts & Locks] Installation is a local atomic file operation; runtime
                    leases are acquired later by the RSI action entry.
[Side-Effect Policy] Creates only the standard wrapper and an absent binding
                     template below the selected RSI root.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


STANDARD_ENTRY = Path("bin/mindie_agent_rsi_entry.py")
STANDARD_ADAPTER = Path("bin/rsi_runtime_adapter.py")
DEFAULT_BINDINGS = Path("config/live-runtime-bindings.json")
_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,191}$")


def _atomic_write(path: Path, content: str, *, executable: bool = False) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # ``Path.write_text`` did not accept ``newline`` on the Python 3.9
    # runtime shipped on some MindIE hosts.  The generated files contain
    # explicit LF text already, so the portable call is sufficient here.
    temporary.write_text(content, encoding="utf-8")
    if executable:
        temporary.chmod(temporary.stat().st_mode | 0o111)
    os.replace(temporary, path)
    return True


def _runtime_entry(root: Path, requested: str | None) -> str:
    candidates = [requested] if requested else [
        "rsi-mindie/bin/mindie_agent_rsi_entry.py",
        "bin/rsi_runtime_entry.py",
        "bin/runtime_entry.py",
    ]
    for value in candidates:
        if isinstance(value, str) and _RELATIVE.fullmatch(value) and ".." not in value.split("/"):
            if (root / value).is_file():
                return value
    return ""


def _copy_core(root: Path) -> int:
    source_root = Path(__file__).parent
    target_root = root / "bin" / "mindie_rsi"
    created = 0
    for source in source_root.glob("*.py"):
        target = target_root / source.name
        if _atomic_write(target, source.read_text(encoding="utf-8")):
            created += 1
    return created


def install(root: str | Path, *, bindings: str | Path | None = None,
            runtime_entry: str | None = None, template: str | None = None) -> dict[str, Any]:
    root_path = Path(root).resolve()
    core_created = _copy_core(root_path)
    entry_path = root_path / STANDARD_ENTRY
    source = (Path(template) if template
              else Path(__file__).parent.parent / "rsi_entry.py")
    if not source.is_file():
        raise FileNotFoundError(f"entry template not found: {source}")
    entry_created = _atomic_write(entry_path, source.read_text(encoding="utf-8"), executable=True)
    adapter_path = root_path / STANDARD_ADAPTER
    adapter_source = Path(__file__).parent.parent / "rsi_runtime_adapter.py"
    if not adapter_source.is_file():
        raise FileNotFoundError(f"runtime adapter template not found: {adapter_source}")
    adapter_created = _atomic_write(
        adapter_path, adapter_source.read_text(encoding="utf-8"), executable=True,
    )
    binding_path = Path(bindings) if bindings else root_path / DEFAULT_BINDINGS
    selected = _runtime_entry(root_path, runtime_entry) or STANDARD_ADAPTER.as_posix()
    binding_created = False
    if not binding_path.exists():
        binding = {
            "schema": "mindie-agent/rsi-bindings/v1",
            "default": {
                "runtime_entry": selected,
                "runtime_python": sys.executable if os.name == "nt" else "python3",
                "commands": {},
                "profile_binding": {
                    "tool": "msprof",
                    "command": ["msprof"],
                    "aic_metrics": ["PipeUtilization"],
                    "source": "container_path_or_PATH",
                },
                "env": {},
                "timeout_s": 3600,
            },
        }
        binding_created = _atomic_write(
            binding_path,
            json.dumps(binding, ensure_ascii=False, indent=2) + "\n",
        )
    return {
        "rsi_root": str(root_path),
        "entry": str(entry_path),
        "entry_created": entry_created,
        "adapter": str(adapter_path),
        "adapter_created": adapter_created,
        "core_files_created": core_created,
        "core_package": str(root_path / "bin" / "mindie_rsi"),
        "bindings": str(binding_path),
        "bindings_created": binding_created,
        "runtime_entry_detected": selected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rsi_root", type=Path)
    parser.add_argument("--bindings", type=Path, default=None)
    parser.add_argument("--runtime-entry", default=None)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.rsi_root, bindings=args.bindings,
                                 runtime_entry=args.runtime_entry), ensure_ascii=False,
                         sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
