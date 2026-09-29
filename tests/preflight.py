#!/usr/bin/env python3
"""Exit 2 if a pinned checkout or installed commit is wrong. No sibling search."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
CHECKOUTS = (
    ("MINDIE_CORE_REPO", "e099a80fd99a6d895579b5d016aecf2a96851559"),
    ("MINDIE_KIMI_REPO", "90f73e76c6087ce091570f2d151b709145c913bc"),
)


def fail(message: str) -> None:
    print("setup: " + message, file=sys.stderr)
    raise SystemExit(2)


def require_commit(env_name: str, commit: str) -> None:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        fail(
            f"{env_name} is unset. Point it at a git checkout that contains {commit}. "
            "This does not search sibling directories."
        )
    path = Path(raw).expanduser()
    if not path.is_dir():
        fail(f"{env_name} is not a directory: {path}")
    kind = subprocess.run(
        ["git", "-C", str(path), "cat-file", "-t", commit],
        capture_output=True, text=True, check=False,
    )
    if kind.returncode != 0 or kind.stdout.strip() != "commit":
        detail = (kind.stderr or kind.stdout).strip()
        fail(f"{env_name}={path} does not contain commit {commit}. {detail}".rstrip())


def installed_commit(dist_name: str) -> str:
    try:
        text = importlib.metadata.distribution(dist_name).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        fail(f"{dist_name} is not installed")
    if not text:
        fail(f"{dist_name} has no direct_url.json")
    data = json.loads(text)
    commit = (data.get("vcs_info") or {}).get("commit_id")
    if not isinstance(commit, str) or not commit:
        fail(f"{dist_name} direct_url.json has no vcs commit")
    return commit


def require_requirement_pins(path: Path) -> None:
    import re
    pattern = re.compile(
        r"([A-Za-z0-9_.-]+) @ git\+https://github.com/mindie-agent/\S+@([0-9a-f]{40})"
    )
    found = pattern.findall(path.read_text(encoding="utf-8"))
    if not found:
        fail(f"{path.name} declares no exact commit pins")
    for name, commit in found:
        actual = installed_commit(name)
        if actual != commit:
            fail(f"installed {name} commit {actual} != required {commit}")
        print(f"{name} {actual}")


def main() -> None:
    for env_name, commit in CHECKOUTS:
        require_commit(env_name, commit)
        print(f"{env_name} contains {commit}")
    require_requirement_pins(REPO / "runtime-requirements.txt")


if __name__ == "__main__":
    main()
