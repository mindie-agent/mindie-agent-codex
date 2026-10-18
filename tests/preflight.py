#!/usr/bin/env python3
"""Exit 2 if an installed runtime commit or product contract is wrong."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/mindie-agent/scripts"))
from product_contract import requirements, probe
from bounded_process import run


def fail(message: str) -> None:
    print("setup: " + message, file=sys.stderr)
    raise SystemExit(2)


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


def require_requirement_pins(source: Path) -> None:
    for name, commit in requirements(source)[0].items():
        actual = installed_commit(name)
        if actual != commit:
            fail(f"installed {name} commit {actual} != required {commit}")
        print(f"{name} {actual}")


def main() -> None:
    require_requirement_pins(REPO)
    receipt = probe(sys.executable, REPO / "plugins/mindie-agent/scripts",
                    lambda argv, **kwargs: run(argv, "", **kwargs).checked_stdout())
    print("validated product " + receipt["product_sha256"])


if __name__ == "__main__":
    main()
