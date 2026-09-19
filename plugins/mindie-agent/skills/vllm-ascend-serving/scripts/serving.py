#!/usr/bin/env python3
"""Start, inspect or stop one managed vLLM service."""
from __future__ import annotations

# Observe the real CLI before optional runtime imports; copied remote helpers stay standalone.

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path.cwd()  # the user's business checkout; no workspace root exists


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('action', choices=('start', 'status', 'stop'))
    parser.add_argument('args', nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    module = importlib.import_module(f'_serving_{args.action}')
    return module.main(args.args)


if __name__ == '__main__':
    raise SystemExit(main())
