import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import rsi_autotune  # noqa: E402
import rsi_plan  # noqa: E402


def plan(tmp_path, *, candidates=None):
    return {
        "schema": rsi_plan.SCHEMA,
        "task_id": "autotune-test",
        "endpoint": {"host": "npu.example", "port": 22},
        "source_root": "/workspace/source",
        "startup_script": "/workspace/source/start.sh",
        "test_script": "/workspace/source/benchmark.sh",
        # The plan schema intentionally accepts remote POSIX paths only;
        # pytest's local Windows temp directory is used solely for state.
        "rsi_root": "/workspace/rsi",
        "baseline": "baseline-control",
        "objective": "verified throughput",
        "operations": ["prepare", "build", "accuracy", "performance", "cleanup"],
        "parallel_workers": 2,
        "devices": ["0", "1"],
        "payload": {"candidates": candidates or [
            {"candidate_id": "c1", "method": "config", "config": {"k": 1}}
        ]},
    }


class ScriptedRunner:
    def __init__(self, *, speedup=1.2, fail=False, unknown=False):
        self.calls = []
        self.speedup = speedup
        self.fail = fail
        self.unknown = unknown

    def run(self, operation, payload):
        self.calls.append(operation)
        if operation == "build" and self.fail:
            return rsi_autotune.ActionReceipt(operation, {"status": "failed"})
        if operation == "performance" and self.unknown:
            return rsi_autotune.ActionReceipt(operation, {"status": "running"})
        if operation == "accuracy":
            return rsi_autotune.ActionReceipt(operation, {
                "status": "completed", "passed": True, "correctness_passed": True,
                "teacher_forced": True,
            })
        if operation == "performance":
            return rsi_autotune.ActionReceipt(operation, {
                "status": "completed", "passed": True,
                "benchmark_passed": True,
                "repeats": 3,
                "paired_speedup_ci_lower_bound": self.speedup,
            })
        return rsi_autotune.ActionReceipt(operation, {"status": "completed"})


def test_expand_candidates_is_deterministic_and_bounded():
    payload = {"candidate_method": "tree", "candidate_space": {"k": [1, 2], "depth": [4, 8]}}
    first = rsi_autotune.expand_candidates(payload)
    second = rsi_autotune.expand_candidates(payload)
    assert [x["candidate_id"] for x in first] == [x["candidate_id"] for x in second]
    assert len(first) == 4


def test_successful_candidate_is_promoted_only_after_all_gates(tmp_path):
    runner = ScriptedRunner()
    controller = rsi_autotune.AutotuneController(
        plan(tmp_path), runner, tmp_path / "state", workers=1,
        required_gates=rsi_autotune.DEFAULT_REQUIRED_GATES,
    )
    result = controller.run_once()
    assert result["best"]["candidate_id"] == "c1"
    assert json.loads((tmp_path / "state" / "active-best.json").read_text())["candidate_id"] == "c1"
    assert runner.calls == ["prepare", "build", "accuracy", "performance"]


def test_failed_candidate_is_cleaned_and_does_not_replace_baseline(tmp_path):
    runner = ScriptedRunner(fail=True)
    controller = rsi_autotune.AutotuneController(plan(tmp_path), runner, tmp_path / "state")
    result = controller.run_once()
    assert result["best"]["candidate_id"] == "baseline-control"
    assert runner.calls[-1] == "cleanup"


def test_unknown_receipt_stops_promotion_without_retry(tmp_path):
    runner = ScriptedRunner(unknown=True)
    controller = rsi_autotune.AutotuneController(plan(tmp_path), runner, tmp_path / "state")
    result = controller.run_once()
    assert result["state"] == "needs_reconcile"
    assert result["best"]["candidate_id"] == "baseline-control"
    assert runner.calls == ["prepare", "build", "accuracy", "performance"]


def test_cli_dry_run_is_machine_readable(tmp_path, capsys):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan(tmp_path)), encoding="utf-8")
    assert rsi_autotune.main([str(path), "--dry-run", "--once", "--state-dir", str(tmp_path / "state")]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "completed"
    assert body["best"]["candidate_id"] == "baseline-control"


def test_cli_requests_missing_startup_or_test_artifacts(tmp_path, capsys):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan(tmp_path)), encoding="utf-8")
    assert rsi_autotune.main([str(path), "--state-dir", str(tmp_path / "state")]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "needs_input"
    assert "startup_script" in body["required_inputs"][0]


def test_cli_stops_when_target_is_reached_by_default(tmp_path, monkeypatch, capsys):
    class FakeController:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def run_once(self):
            type(self).calls += 1
            return {"status": "completed", "best": {
                "candidate_id": f"c{type(self).calls}", "speedup": 1.07}}

    monkeypatch.setattr(rsi_autotune, "AutotuneController", FakeController)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan(tmp_path)), encoding="utf-8")
    assert rsi_autotune.main([
        str(path), "--dry-run", "--target-speedup", "1.07",
        "--state-dir", str(tmp_path / "state"),
    ]) == 0
    assert FakeController.calls == 1
    assert json.loads(capsys.readouterr().out)["best"]["speedup"] == 1.07


def test_cli_explicit_round_budget_keeps_running_after_target(tmp_path, monkeypatch, capsys):
    class FakeController:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def run_once(self):
            type(self).calls += 1
            return {"status": "completed", "best": {
                "candidate_id": f"c{type(self).calls}", "speedup": 1.07}}

    monkeypatch.setattr(rsi_autotune, "AutotuneController", FakeController)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan(tmp_path)), encoding="utf-8")
    assert rsi_autotune.main([
        str(path), "--dry-run", "--target-speedup", "1.07", "--max-rounds", "3",
        "--state-dir", str(tmp_path / "state"),
    ]) == 0
    assert FakeController.calls == 3
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["best"]["candidate_id"] == "c3"
