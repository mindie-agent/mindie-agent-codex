#!/usr/bin/env python3
"""Run a bounded, resumable RSI candidate loop on a customer host.

[Role] Reuses the validated RSI plan to execute candidate waves and promotion.
[Invariants] Candidate state is durable; active-best changes only after all
             requested gates and the paired-speed lower bound pass.
[Timeouts & Locks] The runtime action entry owns NPU leases and job handles;
                    this controller never retries an unknown external result.
[Side-Effect Policy] Writes only controller state and delegates remote work to
                     the fixed RSI action entry; arbitrary shell is not accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from rsi_plan import SCHEMA, PlanError, validate
    from rsi_install import install as install_rsi_entry
    from rsi_cost_model import plan_from_payload
    from rsi_workload import DEFAULT_MEASURED_REPEATS, build_manifest
    from rsi_compare import compare as compare_objective
    from rsi_evidence import validate_paired, validate_profile, validate_teacher_forcing
except ImportError:  # pragma: no cover - direct package execution fallback
    from .rsi_plan import SCHEMA, PlanError, validate
    from .rsi_install import install as install_rsi_entry
    from .rsi_cost_model import plan_from_payload
    from .rsi_workload import DEFAULT_MEASURED_REPEATS, build_manifest
    from .rsi_compare import compare as compare_objective
    from .rsi_evidence import validate_paired, validate_profile, validate_teacher_forcing


STATE_SCHEMA = "mindie-agent/rsi-autotune-state/v1"
UNKNOWN_EXTERNAL = {"unknown", "running", "submitted", "pending", "inflight"}
DEFAULT_REQUIRED_GATES = (
    "benchmark_passed", "correctness_passed",
)


class AutotuneError(ValueError):
    """A plan or receipt cannot be used safely by the controller."""


class UnknownExternalEffect(RuntimeError):
    """An external action may still be running; resubmission is forbidden."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def candidate_id(method: str, config: Mapping[str, Any], patch: str | None = None) -> str:
    raw = _json({"method": method, "config": dict(config), "patch": patch}).encode()
    return "candidate-" + hashlib.sha256(raw).hexdigest()[:16]


def expand_candidates(payload: Mapping[str, Any], *, limit: int = 256) -> list[dict[str, Any]]:
    """Expand explicit candidates or a bounded Cartesian candidate space."""
    explicit = payload.get("candidates")
    if explicit is not None:
        if not isinstance(explicit, list):
            raise AutotuneError("payload.candidates must be an array")
        source: Iterable[Mapping[str, Any]] = (
            item for item in explicit if isinstance(item, Mapping)
        )
        result: list[dict[str, Any]] = []
        for item in source:
            config = dict(item.get("config") or {})
            method = str(item.get("method") or "candidate")
            value = dict(item)
            value.setdefault("candidate_id", candidate_id(method, config, item.get("patch")))
            value.setdefault("method", method)
            value["config"] = config
            value.setdefault("status", "pending")
            result.append(value)
            if len(result) >= limit:
                break
        return result

    space = payload.get("candidate_space") or {}
    if not isinstance(space, Mapping):
        raise AutotuneError("payload.candidate_space must be an object")
    method = str(payload.get("candidate_method") or "candidate")
    keys = [str(key) for key in space]
    values: list[list[Any]] = []
    for key in keys:
        value = space[key]
        if isinstance(value, (list, tuple)):
            values.append(list(value))
        else:
            values.append([value])
    combinations = itertools.product(*values) if values else [()]
    result = []
    for combo in combinations:
        config = dict(zip(keys, combo))
        result.append({"candidate_id": candidate_id(method, config),
                       "method": method, "config": config, "status": "pending"})
        if len(result) >= limit:
            break
    return result


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _status(receipt: Mapping[str, Any]) -> str:
    value = receipt.get("status")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    if receipt.get("error") or receipt.get("failed") is True:
        return "failed"
    return "completed"


def _passed(receipt: Mapping[str, Any], *keys: str) -> bool | None:
    for key in keys:
        if key in receipt:
            return receipt[key] is True
    gates = receipt.get("gates")
    if isinstance(gates, Mapping):
        for key in keys:
            if key in gates:
                return gates[key] is True
    return None


def _lower_bound(receipt: Mapping[str, Any]) -> float | None:
    for key in ("paired_speedup_ci_lower_bound", "speedup_ci_lower_bound",
                "speed_ci_lower_bound", "lower_bound"):
        value = receipt.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    for key in ("paired_speedup", "speedup"):
        value = receipt.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    metrics = receipt.get("metrics")
    if isinstance(metrics, Mapping):
        return _lower_bound(metrics)
    return None


def _binding_key(item: Mapping[str, Any]) -> tuple[str, str]:
    binding = item.get("binding") or item.get("runtime_binding") or item
    if not isinstance(binding, Mapping):
        return "", ""
    devices = binding.get("devices") or binding.get("device") or binding.get("device_pool")
    port = binding.get("candidate_url") or binding.get("port") or ""
    if isinstance(devices, list):
        devices = ",".join(str(x) for x in devices)
    return str(devices), str(port)


@dataclass(frozen=True)
class ActionReceipt:
    operation: str
    value: Mapping[str, Any]


class ActionRunner:
    """Invoke the fixed RSI action entry with JSON on stdin."""

    def __init__(self, root: str, *, python: str = "python3",
                 bindings: str | None = None, timeout_s: int = 3600) -> None:
        self.root = str(root)
        self.python = python
        self.entry = "bin/mindie_agent_rsi_entry.py"
        self.bindings = bindings
        self.timeout_s = timeout_s

    def run(self, operation: str, payload: Mapping[str, Any]) -> ActionReceipt:
        command = [self.python, self.entry, operation, "--root", self.root]
        if self.bindings:
            command.extend(("--bindings", self.bindings))
        try:
            process = subprocess.run(command, input=_json(payload), text=True,
                                     capture_output=True, timeout=self.timeout_s,
                                     check=False, cwd=self.root)
        except subprocess.TimeoutExpired as exc:
            raise UnknownExternalEffect(f"{operation} timed out") from exc
        if process.returncode not in (0, 2):
            raise AutotuneError(process.stderr.strip() or f"{operation} failed")
        for line in reversed((process.stdout or "").splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping):
                receipt = dict(value)
                if _status(receipt) in UNKNOWN_EXTERNAL:
                    raise UnknownExternalEffect(f"{operation} returned {_status(receipt)}")
                return ActionReceipt(operation, receipt)
        return ActionReceipt(operation, {"status": "completed", "stdout": process.stdout})


class DryRunRunner:
    def run(self, operation: str, payload: Mapping[str, Any]) -> ActionReceipt:
        return ActionReceipt(operation, {"status": "dry-run", "operation": operation,
                                          "payload": dict(payload)})


class AutotuneController:
    def __init__(self, plan: Mapping[str, Any], runner: Any, state_dir: str | Path,
                 *, workers: int = 1, min_speedup: float = 1.0,
                 min_relative_improvement: float = 0.0,
                 required_gates: Sequence[str] = DEFAULT_REQUIRED_GATES) -> None:
        self.plan = validate(plan)
        self.runner = runner
        self.state_dir = Path(state_dir)
        self.workers = max(1, min(int(workers), 128))
        self.min_speedup = float(min_speedup)
        self.min_relative_improvement = max(0.0, float(min_relative_improvement))
        self.required_gates = tuple(str(x) for x in required_gates)
        self.state_path = self.state_dir / "autotune-state.json"
        self.queue_path = self.state_dir / "autotune-queue.json"
        self.active_path = self.state_dir / "active-best.json"
        self.cost_model_path = self.state_dir / "cost-model.json"
        self.workload_path = self.state_dir / "workload-manifest.json"
    def _deployment_signature(self) -> str:
        endpoint = self.plan["endpoint"]
        value = {"host": endpoint["host"], "port": endpoint["port"],
                 "source_root": self.plan["source_root"],
                 "rsi_root": self.plan["rsi_root"],
                 "baseline": self.plan["baseline"],
                 "devices": self.plan["devices"]}
        return hashlib.sha256(_json(value).encode()).hexdigest()[:24]

    def _prepare_metadata(self, state: dict[str, Any]) -> None:
        """Persist the cost-model and expose replayable memory to runners."""
        startup_text = None
        try:
            startup_text = Path(self.plan.get("startup_script", "")).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
        manifest = build_manifest({**self.plan["payload"], "objective": self.plan["objective"],
                                   "test_command": self.plan.get("test_command")},
                                  startup_text=startup_text)
        state["workload_manifest"] = manifest
        _write(self.workload_path, manifest)
        report = plan_from_payload(self.plan["payload"])
        if report is not None:
            state["cost_model"] = report
            _write(self.cost_model_path, report)
        else:
            cached = _read(self.cost_model_path, None)
            if isinstance(cached, dict):
                state["cost_model"] = cached

    def _load_state(self) -> dict[str, Any]:
        value = _read(self.state_path, {})
        if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
            return {"schema": STATE_SCHEMA, "status": "idle", "round": 0,
                    "best": {"candidate_id": "baseline-control", "speedup": 1.0},
                    "evaluated": {}, "history": []}
        value.setdefault("evaluated", {})
        value.setdefault("history", [])
        value.setdefault("best", {"candidate_id": "baseline-control", "speedup": 1.0})
        return value

    def _load_queue(self, state: Mapping[str, Any]) -> dict[str, Any]:
        old = _read(self.queue_path, {})
        if isinstance(old, dict) and isinstance(old.get("candidates"), list):
            return old
        candidate_payload = self.plan["payload"]
        startup_parameters = (state.get("workload_manifest") or {}).get("startup_parameters", {})
        if isinstance(startup_parameters, Mapping) and "num_speculative_tokens" in startup_parameters:
            candidate_payload = {
                **candidate_payload,
                "current_num_speculative_tokens": startup_parameters["num_speculative_tokens"],
            }
        candidates = expand_candidates(candidate_payload,
                                       limit=int(self.plan["payload"].get("max_candidates", 256)))
        queue = {"schema": "mindie-agent/rsi-autotune-queue/v1",
                 "candidates": candidates, "counts": {"pending": len(candidates)}}
        _write(self.queue_path, queue)
        return queue

    def _payload(self, item: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
        controls = dict(self.plan["payload"])
        payload = {"schema": SCHEMA, "task_id": self.plan["task_id"],
                "endpoint": self.plan["endpoint"], "source_root": self.plan["source_root"],
                "rsi_root": self.plan["rsi_root"], "baseline": self.plan["baseline"],
                "objective": self.plan["objective"], "devices": self.plan["devices"],
                "startup_script": self.plan["startup_script"],
                "test_script": self.plan.get("test_script"),
                "test_config": self.plan.get("test_config"),
                "test_description": self.plan.get("test_description"),
                "test_command": self.plan.get("test_command"),
                "bindings": self.plan.get("bindings"), "candidate": dict(item),
                "current_best": dict(state.get("best") or {}),
                "cost_model": state.get("cost_model"),
                "workload_manifest": state.get("workload_manifest"),
                "controls": controls}
        payload["defaults"] = dict(self.plan.get("defaults") or {})
        # Runtime adapters receive a stable top-level action payload.  Keep the
        # complete plan under ``controls`` for auditability, while forwarding
        # the concrete deployment and command bindings needed by legacy
        # adapters that do not inspect the nested controls object.
        for key in ("deployment", "build_cmd", "profile_cmd", "token_forcing_cmd",
                    "benchmark_cmd", "teacher_forcing_scorer_cmd", "profile_paths", "msprof_paths",
                    "token_forcing_rows", "candidate_token_ids", "stock_token_ids", "margin_delta",
                    "workload_manifest", "warmup_runs", "repeats", "dataset", "dataset_revision",
                    "prompt_ids", "batch_size", "concurrency", "max_new_tokens", "temperature", "top_p",
                    "seed", "model_revision", "tokenizer_revision", "timeout_s"):
            if key in controls:
                payload[key] = controls[key]
        return payload

    def _operations(self) -> list[str]:
        ordered = ("prepare", "build", "profile", "accuracy", "performance")
        return [operation for operation in ordered if operation in self.plan["operations"]]

    def _evaluate(self, item: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
        precheck = item.get("theoretical_precheck")
        if isinstance(precheck, Mapping) and precheck.get("eligible_for_measurement") is False:
            return {"status": "failed", "reason": "theoretical_precheck_failed",
                    "theoretical_precheck": dict(precheck), "candidate_id": item.get("candidate_id")}
        payload = self._payload(item, state)
        receipts: dict[str, Mapping[str, Any]] = {}
        try:
            for operation in self._operations():
                receipt = self.runner.run(operation, payload)
                receipts[operation] = dict(receipt.value)
                if operation == "prepare" and isinstance(receipt.value.get("workload_snapshot"), Mapping):
                    snapshot = dict(receipt.value["workload_snapshot"])
                    manifest = dict(payload.get("workload_manifest") or {})
                    manifest.update({key: value for key, value in snapshot.items() if value})
                    payload["workload_manifest"] = manifest
                status = _status(receipt.value)
                if status in UNKNOWN_EXTERNAL:
                    raise UnknownExternalEffect(f"{operation} returned {status}")
                if status in {"failed", "error"} or receipt.value.get("passed") is False:
                    self._cleanup(payload, receipts)
                    return {"status": "failed", "reason": f"{operation}_failed",
                            "receipts": receipts}
        except UnknownExternalEffect as exc:
            return {"status": "unknown", "reason": str(exc), "receipts": receipts,
                    "requires_reconcile": True}
        except Exception as exc:
            self._cleanup(payload, receipts)
            return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}",
                    "receipts": receipts}

        accuracy = receipts.get("accuracy", {})
        performance = receipts.get("performance", {})
        objective_comparison = None
        if isinstance(performance, Mapping):
            if performance.get("paired_runs") or performance.get("baseline_samples"):
                objective_comparison = compare_objective(
                    performance, self.plan["objective"], self.plan["payload"])
        profile_report = receipts.get("profile", {})
        cost_model = None
        profile_rows = profile_report.get("profile_observations") if isinstance(profile_report, Mapping) else None
        if isinstance(profile_rows, list) and profile_rows:
            model_payload = dict(self.plan["payload"])
            model_payload["profile_observations"] = profile_rows
            model_payload["profile"] = profile_report
            cost_model = plan_from_payload(model_payload)
        strict_evidence = bool(self.plan["payload"].get("strict_evidence"))
        evidence: dict[str, Any] = {}
        if strict_evidence:
            if "profile" in self._operations():
                evidence["profile"] = validate_profile(
                    receipts.get("profile", {}),
                    min_samples=int(self.plan["payload"].get("min_profile_samples", 5)),
                )
            if "accuracy" in self._operations():
                evidence["teacher_forcing"] = validate_teacher_forcing(
                    accuracy,
                    min_rows=int(self.plan["payload"].get("min_teacher_forcing_rows", 1)),
                )
            if "performance" in self._operations():
                evidence["paired_performance"] = validate_paired(
                    performance,
                    min_runs=int(self.plan["payload"].get("min_paired_runs", 5)),
                )
        gates: dict[str, bool] = {
            "benchmark_passed": _passed(performance, "benchmark_passed", "passed") is True,
            "correctness_passed": _passed(accuracy, "correctness_passed", "passed") is True,
            "teacher_forced": _passed(accuracy, "teacher_forced") is True,
        }
        if strict_evidence:
            if "profile" in evidence:
                gates["profile_evidence"] = evidence["profile"]["passed"]
            if "teacher_forcing" in evidence:
                gates["teacher_forced"] = gates["teacher_forced"] and evidence["teacher_forcing"]["passed"]
            if "paired_performance" in evidence:
                gates["paired_evidence"] = evidence["paired_performance"]["passed"]
        lower_bound = _lower_bound(performance)
        if objective_comparison and isinstance(objective_comparison.get("ci_lower_ratio"), (int, float)):
            lower_bound = float(objective_comparison["ci_lower_ratio"])
        gates["paired_speed_ci"] = lower_bound is not None and lower_bound > self.min_speedup
        measured_repeats = performance.get("repeats") if isinstance(performance, Mapping) else None
        if not isinstance(measured_repeats, int):
            runs = performance.get("runs") if isinstance(performance, Mapping) else None
            measured_repeats = len(runs) if isinstance(runs, list) else 0
        gates["repeated_measurements"] = measured_repeats >= DEFAULT_MEASURED_REPEATS
        passed = (all(gates.get(name, False) for name in self.required_gates)
                  and gates["paired_speed_ci"] and gates["repeated_measurements"])
        result = {"status": "passed" if passed else "evaluated", "gates": gates,
                "paired_speedup_ci_lower_bound": lower_bound, "receipts": receipts,
                "candidate_id": item["candidate_id"],
                "workload_manifest": payload.get("workload_manifest")}
        if cost_model is not None:
            result["cost_model"] = cost_model
        if objective_comparison is not None:
            result["objective_comparison"] = objective_comparison
        if evidence:
            result["evidence"] = evidence
        return result

    def _cleanup(self, payload: Mapping[str, Any], receipts: Mapping[str, Any]) -> None:
        if "cleanup" not in self.plan["operations"]:
            return
        try:
            self.runner.run("cleanup", {**payload, "receipts": dict(receipts)})
        except Exception:
            # Cleanup failure is reflected in the candidate result by the caller;
            # never replace the original failed/unknown action with a retry.
            pass

    def _select_wave(self, queue: Mapping[str, Any], state: Mapping[str, Any]) -> list[dict[str, Any]]:
        evaluated = set((state.get("evaluated") or {}).keys())
        selected: list[dict[str, Any]] = []
        occupied: set[tuple[str, str]] = set()
        for raw in queue.get("candidates", []):
            if not isinstance(raw, Mapping) or raw.get("status") not in {"pending", "bound"}:
                continue
            cid = str(raw.get("candidate_id"))
            if cid in evaluated:
                continue
            key = _binding_key(raw)
            if key in occupied and key != ("", ""):
                continue
            occupied.add(key)
            selected.append(dict(raw))
            if len(selected) >= self.workers:
                break
        return selected

    def run_once(self) -> dict[str, Any]:
        state = self._load_state()
        self._prepare_metadata(state)
        queue = self._load_queue(state)
        wave = self._select_wave(queue, state)
        if not wave:
            return {"status": "idle", "reason": "no_pending_candidates", "best": state.get("best")}
        state["status"] = "running"
        state["round"] = int(state.get("round", 0)) + 1
        _write(self.state_path, state)
        results: dict[str, dict[str, Any]] = {}
        if len(wave) == 1:
            item = wave[0]
            results[item["candidate_id"]] = self._evaluate(item, state)
        else:
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                futures = {pool.submit(self._evaluate, item, state): item for item in wave}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        results[item["candidate_id"]] = future.result()
                    except Exception as exc:
                        results[item["candidate_id"]] = {
                            "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}

        best = dict(state.get("best") or {"candidate_id": "baseline-control", "speedup": 1.0})
        for item in wave:
            cid = item["candidate_id"]
            result = results.get(cid, {"status": "failed", "reason": "missing_result"})
            if isinstance(result.get("workload_manifest"), Mapping):
                state["workload_manifest"] = dict(result["workload_manifest"])
                _write(self.workload_path, state["workload_manifest"])
            if isinstance(result.get("cost_model"), Mapping):
                state["cost_model"] = dict(result["cost_model"])
                _write(self.cost_model_path, state["cost_model"])
            old_speedup = float(best.get("speedup") or 1.0)
            candidate_speedup = float(result.get("paired_speedup_ci_lower_bound") or 0.0)
            promoted = bool(result.get("status") == "passed" and candidate_speedup > old_speedup * (1 + self.min_relative_improvement))
            for queue_item in queue.get("candidates", []):
                if isinstance(queue_item, dict) and queue_item.get("candidate_id") == cid:
                    queue_item["status"] = "promoted" if promoted else result.get("status", "failed")
                    queue_item["result"] = result
                    break
            state.setdefault("evaluated", {})[cid] = {"status": "promoted" if promoted else result.get("status", "failed"),
                                                       "round": state["round"],
                                                       "speedup": candidate_speedup}
            state.setdefault("history", []).append({"candidate_id": cid,
                                                      "round": state["round"],
                                                      "status": "promoted" if promoted else result.get("status", "failed"),
                                                      "speedup": candidate_speedup})
            if promoted:
                best = {"candidate_id": cid, "method": item.get("method"),
                        "speedup": candidate_speedup, "binding": item.get("binding") or item.get("runtime_binding") or {},
                        "round": state["round"], "updated_unix": time.time()}
        state["best"] = best
        state["active_candidate"] = None
        state["status"] = "idle" if not any(x.get("status") == "unknown" for x in results.values()) else "needs_reconcile"
        queue["counts"] = {}
        for item in queue.get("candidates", []):
            status = str(item.get("status", "pending"))
            queue["counts"][status] = queue["counts"].get(status, 0) + 1
        queue["updated_unix"] = time.time()
        _write(self.queue_path, queue)
        _write(self.active_path, best)
        _write(self.state_path, state)
        return {"status": "completed", "round": state["round"], "results": results,
                "best": best, "state": state["status"]}


def load_plan(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(str(exc)) from exc
    return validate(value)


def _missing_input_files(plan: Mapping[str, Any]) -> list[str]:
    """Check required customer artifacts on the host running the RSI loop."""
    missing = []
    for field in ("startup_script", "test_script", "test_config"):
        value = plan.get(field)
        if value and not Path(value).is_file():
            missing.append(f"{field}:{value}")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--parallel-workers", type=int, default=None)
    parser.add_argument("--min-speedup", type=float, default=None)
    parser.add_argument("--min-relative-improvement", type=float, default=None)
    parser.add_argument("--max-rounds", type=int, default=None,
                        help="Run exactly this many rounds; disables target early-stop.")
    parser.add_argument("--target-speedup", type=float, default=None,
                        help="Stop early when the promoted best reaches this CI lower bound.")
    parser.add_argument("--stop-when-target-reached", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Enable/disable default target early-stop behavior.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        payload = plan["payload"]
        if not args.dry_run:
            missing_files = _missing_input_files(plan)
            if missing_files:
                print(json.dumps({"schema": STATE_SCHEMA, "status": "needs_input",
                                  "required_inputs": missing_files,
                                  "error": "required startup/test artifact not found"},
                                 ensure_ascii=False))
                return 2
        state_dir = args.state_dir or Path(plan["rsi_root"]) / ".mindie-agent" / plan["task_id"]
        workers = args.parallel_workers or plan["parallel_workers"]
        minimum = args.min_speedup if args.min_speedup is not None else float(payload.get("min_speedup", 1.0))
        improvement = (args.min_relative_improvement if args.min_relative_improvement is not None
                       else float(payload.get("min_relative_improvement", 0.0)))
        target = (args.target_speedup if args.target_speedup is not None
                  else payload.get("target_speedup", payload.get("min_speedup")))
        if target is not None:
            target = float(target)
            if target <= 0:
                raise AutotuneError("target_speedup must be positive")
        stop_when_target = (args.stop_when_target_reached
                            if args.stop_when_target_reached is not None
                            else bool(payload.get("stop_when_target_reached", True)))
        required = payload.get("required_gates", list(DEFAULT_REQUIRED_GATES))
        if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
            raise AutotuneError("payload.required_gates must be an array of strings")
        correctness_mode = str(payload.get("correctness_mode") or
                               plan.get("defaults", {}).get("correctness_mode") or
                               "task_accuracy")
        if correctness_mode == "task_accuracy":
            required = [gate for gate in required if gate != "teacher_forced"]
        elif correctness_mode != "lossless_token_forcing":
            raise AutotuneError(
                "correctness_mode must be task_accuracy or lossless_token_forcing"
            )
        if args.dry_run:
            runner = DryRunRunner()
        else:
            install_rsi_entry(plan["rsi_root"], bindings=plan.get("bindings"))
            runner = ActionRunner(plan["rsi_root"], bindings=plan.get("bindings"))
        controller = AutotuneController(plan, runner, state_dir, workers=workers,
                                        min_speedup=minimum,
                                        min_relative_improvement=improvement,
                                        required_gates=required)
        rounds = 0
        # An explicit round limit is an instruction to finish the requested
        # budget even when the target is reached.  Without one, the default is
        # target-driven and stops as soon as a promoted best clears the target.
        round_limit = args.max_rounds
        explicit_round_limit = round_limit is not None
        if round_limit is None and "max_rounds" in payload:
            round_limit = int(payload["max_rounds"])
            explicit_round_limit = True
        if round_limit is not None and round_limit < 1:
            raise AutotuneError("max_rounds must be at least 1")
        while True:
            result = controller.run_once()
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            rounds += 1
            if args.once:
                break
            if round_limit is not None and rounds >= round_limit:
                break
            if result.get("status") in {"idle", "needs_reconcile"}:
                break
            best = result.get("best")
            reached = (target is not None and isinstance(best, Mapping)
                       and isinstance(best.get("speedup"), (int, float))
                       and float(best["speedup"]) >= target)
            if stop_when_target and not explicit_round_limit and reached:
                break
        return 0
    except (OSError, PlanError, AutotuneError) as exc:
        print(json.dumps({"schema": STATE_SCHEMA, "status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
