"""Legacy native updater activation handoff shim (`mindie-activation-attempt/1`).
[Role] Accept `release_activation.py <config> <release> [--attempt]` from pre-v0.1.38 `harness_update.py`, enforce compare-and-swap against `expected_binding`, and atomically rebind `codex.json` via `ensure_knowledge_binding(..., retire_previous=True, already_locked=True)`.
[Invariants] Never mutates canonical config when `expected_binding` differs (`reason="installation_changed"`), `.codex.json.update.lock` is busy (`reason="operation_busy"`), or retirement/binding is not `"configured"`.
[Timeouts & Locks] Non-blocking `.codex.json.update.lock` under `--attempt` (`15s` without `--attempt`); `64 KiB` stdin cap.
[Side-Effect Policy] Provisions content-addressed 9-domain engines and updates `codex.json` only after CAS + retirement verification.
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
    from mindie_harness.paths import HarnessError, ProfileRef
    args = list(sys.argv if argv is None else argv); config, release, extra = Path(args[1]).absolute(), Path(args[2]).absolute(), args[3:]
    if extra and extra != ["--attempt"]: raise ValueError("activation requires the --attempt snapshot input")
    expected = _attempt_expected(stdin) if extra == ["--attempt"] else None
    receipt = entry._read(release / "runtime.json")
    if receipt.get("python") != sys.executable: raise ValueError("candidate preparation must run in its exact installed runtime")
    started = time.monotonic(); lock = FileLock(config.with_name(f".{config.name}.update.lock"), exclusive=True, timeout=0.0 if expected is not None else 15.0)
    try: lock.acquire()
    except LockTimeoutError: return {"status": "deferred", "reason": "operation_busy", "execution": "not_started"}
    try:
        current = entry._read(config); binding = runtime_binding(current)
        compared = {k: v for k, v in binding.items() if k != "component_commands"} if expected is not None and "component_commands" not in expected else binding
        if expected is not None and compared != expected:
            return {"status": "active", "unavailable_seconds": 0.0} if (current.get("python") == sys.executable and current.get("runtime_scripts") == str(SCRIPTS) and current.get("component_release") == str(release)) else {"status": "deferred", "reason": "installation_changed", "execution": "not_started"}
        source = Path(receipt["source"]) if isinstance(receipt.get("source"), str) and Path(receipt["source"]).is_dir() else SCRIPTS.parent
        profile = ProfileRef.resolve("codex", plugin_root=source, config_path=config)
        try: res = entry._rebind_unlocked(profile, source=source, release=release, version=entry._current_version(source), current=current, retire_previous=True)
        except TimeoutError: raise
        except HarnessError as exc:
            busy = "busy" in exc.reason or "deferred" in exc.reason
            return {"status": "deferred" if busy else "incomplete", "reason": "service_busy" if busy else "old_retirement_unconfirmed", "execution": "not_started"}
        if isinstance(res, dict) and res.get("status") in {"deferred", "incomplete", "skipped", "failed"}:
            return {"status": "deferred" if res["status"] == "deferred" else "incomplete", "reason": str(res.get("reason") or ("service_busy" if res["status"] == "deferred" else "old_retirement_unconfirmed")), "execution": "not_started"}
        return {"status": "active", "unavailable_seconds": time.monotonic() - started}
    finally: lock.release()
if __name__ == "__main__": print(json.dumps(main(), sort_keys=True))
