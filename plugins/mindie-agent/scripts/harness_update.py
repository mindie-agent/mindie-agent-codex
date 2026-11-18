"""Legacy pre-0.1.38 `harness_update.py` background maintenance shim (Issues `#381`, `#382`).
[Role] Accept `harness_update.py [--config <path>]` from in-flight `v0.1.34`–`v0.1.37` `update_trigger._run` threads after `codex.json` switches to `0.1.39+`, delegating to `mindie_harness.runtime.wheelhouse.update_runtime`.
[Invariants] Always exits `0` with a JSON dict without `status_receipt_error_type` (`reportable=False`).
[Timeouts & Locks] Delegates single-instance locking and backoff to `update_runtime`.
[Side-Effect Policy] Delegates runtime wheelhouse checks only.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path: sys.path.insert(0, str(SCRIPTS))
def main(argv: list[str] | None = None) -> int:
    from host_adapter import CodexHostAdapter
    from mindie_harness.runtime.wheelhouse import update_runtime
    parser = argparse.ArgumentParser(prog="harness_update.py")
    parser.add_argument("--config", default=None)
    parser.add_argument("--force", action="store_true")
    opts, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    profile = CodexHostAdapter(config_path=opts.config).resolve_profile(opts.config)
    print(json.dumps(update_runtime(profile, force=opts.force, background=True), sort_keys=True))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
