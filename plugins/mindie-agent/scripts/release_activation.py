"""Legacy native updater activation handoff shim (`mindie-activation-attempt/1`).
[Role] Accept `release_activation.py <config> <release> [--attempt]` from pre-v0.1.38 `harness_update.py`, enforce compare-and-swap against `expected_binding`, and atomically rebind `codex.json`.
[Invariants] Never mutates canonical config when `expected_binding` differs (`reason="installation_changed"`) or `.update.lock` is busy (`reason="operation_busy"`).
[Timeouts & Locks] Non-blocking `.update.lock` under `--attempt` (`15s` without `--attempt`); `15s` idle service retirement; `64 KiB` stdin cap.
[Side-Effect Policy] Atomically updates `codex.engine.json` and `codex.json` only after CAS verification.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path: sys.path.insert(0, str(SCRIPTS))
ATTEMPT_SCHEMA = "mindie-activation-attempt/1"
def runtime_binding(config: dict) -> dict:
    binding = {key: config.get(key) for key in ("python", "runtime_scripts", "engine_config", "product_validation")}
    binding.update({key: config[key] for key in ("component_release", "domain_configs") if key in config})
    binding["component_commands"] = config.get("component_commands", {}); return binding
def _attempt_expected(stream=None) -> dict:
    raw = (sys.stdin.buffer if stream is None else stream).read(65537)
    if not raw or len(raw) > 65536: raise ValueError("activation attempt input is missing or oversized")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != ATTEMPT_SCHEMA or not isinstance(value.get("expected_binding"), dict):
        raise ValueError("activation attempt input is invalid")
    return value["expected_binding"]
def main(argv: list[str] | None = None, stdin=None) -> dict:
    import entry
    from mindie_harness.locks import FileLock, LockTimeoutError
    args = list(sys.argv if argv is None else argv); config, release, extra = Path(args[1]).absolute(), Path(args[2]).absolute(), args[3:]
    if extra and extra != ["--attempt"]: raise ValueError("activation requires the --attempt snapshot input")
    expected = _attempt_expected(stdin) if extra == ["--attempt"] else None
    receipt = entry._read(release / "runtime.json")
    if receipt.get("python") != sys.executable: raise ValueError("candidate preparation must run in its exact installed runtime")
    started = time.monotonic(); lock = FileLock(config.with_suffix(".update.lock"), exclusive=True, timeout=0.0 if expected is not None else 15.0)
    try: lock.acquire()
    except LockTimeoutError: return {"status": "deferred", "reason": "operation_busy", "execution": "not_started"}
    try:
        current = entry._read(config); binding = runtime_binding(current)
        compared = {k: v for k, v in binding.items() if k != "component_commands"} if expected is not None and "component_commands" not in expected else binding
        if expected is not None and compared != expected:
            if current.get("python") == sys.executable and current.get("runtime_scripts") == str(SCRIPTS) and current.get("component_release") == str(release):
                return {"status": "active", "unavailable_seconds": 0.0}
            return {"status": "deferred", "reason": "installation_changed", "execution": "not_started"}
        old_ep = current.get("engine_config")
        if isinstance(old_ep, str) and Path(old_ep).is_file():
            old_eng = entry._read(Path(old_ep))
            old_root, old_dom = old_eng.get("root"), old_eng.get("domain")
            if isinstance(old_root, str) and old_root and isinstance(old_dom, str) and old_dom and (Path(old_root) / old_dom).is_dir():
                from mindie_knowledge.loop.lifecycle import retire_service
                try: retirement = retire_service(Path(old_ep), control_timeout=15.0)
                except TimeoutError: raise
                except (OSError, ValueError): return {"status": "incomplete", "reason": "old_retirement_unconfirmed", "execution": "not_started"}
                if retirement.get("idle") is not True: return {"status": "deferred", "reason": "service_busy", "execution": "not_started"}
                if retirement.get("cleanup_failed"): return {"status": "incomplete", "reason": "old_retirement_unconfirmed", "execution": "not_started"}
        source = Path(receipt["source"]) if isinstance(receipt.get("source"), str) and Path(receipt["source"]).is_dir() else SCRIPTS.parent
        engine_path = config.with_name(config.stem + ".engine.json")
        entry._rebind_unlocked(config, engine_path, source=source, release=release, version=entry._current_version(source), current=current)
        return {"status": "active", "unavailable_seconds": time.monotonic() - started}
    finally:
        lock.release()
if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True))
