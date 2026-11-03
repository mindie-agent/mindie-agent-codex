"""The RSI loop advances one checked step at a time, resumes, gates, and records through MindIE Bench."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import experiment_ledger as ledger  # noqa: E402
import rsi_loop as loop  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=15)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    (root / "kernel.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def state(tmp_path, repo):
    path = tmp_path / "work" / "rsi-loop.json"
    loop.init(path, task_id="t1", ledger_path=tmp_path / "work" / "experiment-ledger.jsonl",
              baseline_repo=repo, direction="maximize", repetitions=2, max_rounds=3)
    return path


def ledger_of(path):
    return ledger.read_ledger(path.parent / "experiment-ledger.jsonl")


def trials(*values, status="completed"):
    return [dict(status=status, metric=v) for v in values]


def to_measure(path, repo, change, exp_id="e1", **propose):
    if loop.next_step(path)["step"] == "profile":
        loop.complete(path, "profile", dict(bottleneck_summary=dict(op="rmsnorm", share=0.31),
                                            profile_ref="profile/r0"))
    mode = loop.next_step(path)["mode"]
    loop.complete(path, "consult", dict(mode=mode, unit_refs=[dict(unit_id="u1", revision=2)]))
    (repo / "kernel.py").write_text(change, encoding="utf-8")
    return loop.complete(path, "propose", dict(dict(exp_id=exp_id, hypothesis="fuse rmsnorm",
                                                    candidate_repo=str(repo),
                                                    scope_conditions=dict(soc="910B")),
                                               **propose))


def measured(**changes):
    body = dict(run_ids=["r1", "r2"], baseline=trials(100.0, 100.0),
                candidate=trials(120.0, 118.0), correctness="passed", card_seconds=40)
    body.update(changes)
    return body


def test_supported_round_is_recorded_by_bench_and_moves_baseline(state, repo):
    result, code = to_measure(state, repo, "x = 2\n")
    assert code == 0 and result["step"] == "measure"
    result, _ = loop.complete(state, "measure", measured())
    assert result["step"] == "learn" and result["verdict"] == "supported"
    result, _ = loop.complete(state, "learn", dict(distill=dict(unit_id="u9", revision=1)))
    assert result["round"] == 2 and result["step"] == "profile"
    assert "pending_distill" not in result
    record = ledger_of(state)[0]
    assert record["verdict"] == "supported" and record["gates"]["gate4_distill_or_refute"]
    assert record["task_id"] == "t1" and record["run_ids"] == ["r1", "r2"]
    assert record["receipt_refs"]["profile"] == "profile/r0"
    assert record["lesson"]["distill"] == dict(unit_id="u9", revision=1)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["baseline"]["exp_id"] == "e1"
    progress = Path(result["progress"]).read_text(encoding="utf-8")
    assert result["progress"] == str(state.parent / "PROGRESS.md")
    assert progress.startswith("# RSI Progress: t1") and "Optimize t1" in progress
    assert "- candidate: `e1`" in progress


def test_out_of_order_step_is_rejected_and_resume_points_to_current(state):
    with pytest.raises(ValueError, match="out of order"):
        loop.complete(state, "measure", measured())
    assert loop.next_step(state)["step"] == "profile"
    loop.complete(state, "profile", dict(bottleneck_summary=dict(op="a"), profile_ref="p"))
    resumed = loop.next_step(state)
    assert resumed["step"] == "consult" and "knowledge_query" in resumed["guide"]


def test_duplicate_source_is_not_submitted_again(state, repo):
    to_measure(state, repo, "x = 2\n")
    loop.complete(state, "measure", measured(candidate=trials(90.0, 91.0)))
    loop.complete(state, "learn", {})
    assert ledger_of(state)[0]["verdict"] == "refuted"
    result, code = to_measure(state, repo, "x = 2\n", exp_id="e2")
    assert code == loop.BLOCKED and result["gate"]["decision"] == "duplicate"
    assert result["step"] == "propose" and result["skipped_this_round"][0]["exp_id"] == "e2"


def test_budget_exhausted_finishes_the_loop(tmp_path, repo):
    path = tmp_path / "b" / "rsi-loop.json"
    loop.init(path, task_id="t1", ledger_path=tmp_path / "b" / "ledger.jsonl",
              baseline_repo=repo, direction="maximize", repetitions=2, max_runs=2)
    to_measure(path, repo, "x = 2\n")
    loop.complete(path, "measure", measured(candidate=trials(90.0, 91.0)))
    loop.complete(path, "learn", {})
    result, code = to_measure(path, repo, "x = 3\n", exp_id="e2")
    assert code == loop.BLOCKED and result["status"] == "finished"
    assert result["finish_reason"].startswith("budget_exhausted")


def test_code_changed_after_gate_is_not_judged(state, repo):
    to_measure(state, repo, "x = 2\n")
    (repo / "kernel.py").write_text("x = 5\n", encoding="utf-8")
    result, code = loop.complete(state, "measure", measured())
    assert code == 1 and "changed after the gate" in result["error"]
    assert result["step"] == "propose"
    assert ledger_of(state) == []


@pytest.mark.parametrize("status", ["infra_failed", "compile_failed"])
def test_execution_failure_is_inconclusive_in_the_bench_entry(state, repo, status):
    to_measure(state, repo, "x = 2\n")
    result, _ = loop.complete(state, "measure", measured(run_status=status))
    assert result["verdict"] == "inconclusive"
    assert result["entry"]["outcome"] in ("measurement_invalid", "compile_failed")
    loop.complete(state, "learn", dict(note="ssh dropped mid-run; rerun"))
    record = ledger_of(state)[0]
    assert record["verdict"] == "inconclusive" and record["run_status"] == status
    progress = (state.parent / "PROGRESS.md").read_text(encoding="utf-8")
    assert "## Experiments To Complete\n\n- `e1`" in progress


def test_three_stalled_rounds_switch_next_round_to_explore(tmp_path, repo):
    path = tmp_path / "s" / "rsi-loop.json"
    loop.init(path, task_id="t1", ledger_path=tmp_path / "s" / "ledger.jsonl",
              baseline_repo=repo, direction="maximize", repetitions=2)
    for index in range(3):
        assert loop.next_step(path)["mode"] == "reuse"
        to_measure(path, repo, f"x = {index + 10}\n", exp_id=f"e{index}")
        # The baseline run is reused every round; rounds must still count separately.
        result, _ = loop.complete(path, "measure", measured(run_ids=["base", f"r{index}"],
                                                            candidate=trials(100.5, 100.0)))
        assert result["entry"]["outcome"] == "speedup_within_noise"
        loop.complete(path, "learn", {})
    resumed = loop.next_step(path)
    assert resumed["mode"] == "explore" and resumed["step"] == "consult"
    assert resumed["profile_reused_from_round"] == 1
    assert "subagent_fact_pack" in resumed["guide"]
    with pytest.raises(ValueError, match="explore"):
        loop.complete(path, "consult", dict(mode="reuse", unit_refs=[]))


def test_new_observation_counts_as_progress(tmp_path, repo):
    path = tmp_path / "o" / "rsi-loop.json"
    loop.init(path, task_id="t1", ledger_path=tmp_path / "o" / "ledger.jsonl",
              baseline_repo=repo, direction="maximize", repetitions=2)
    for index in range(3):
        to_measure(path, repo, f"x = {index + 10}\n", exp_id=f"e{index}")
        loop.complete(path, "measure", measured(run_ids=[f"r{index}"],
                                                candidate=trials(100.5, 100.0)))
        loop.complete(path, "learn", dict(new_observation=index == 2))
    assert loop.next_step(path)["mode"] == "reuse"


def test_unchanged_baseline_reuses_profile_and_kept_gain_reprofiles(state, repo):
    to_measure(state, repo, "x = 2\n")
    loop.complete(state, "measure", measured(candidate=trials(90.0, 91.0)))
    result, _ = loop.complete(state, "learn", {})
    assert result["step"] == "consult" and "restore_hint" in result
    assert result["profile_reused_from_round"] == 1
    git(repo, "checkout", "--", "kernel.py")
    to_measure(state, repo, "x = 3\n", exp_id="e2")
    loop.complete(state, "measure", measured(run_ids=["r3", "r4"]))
    result, _ = loop.complete(state, "learn", dict(distill=dict(unit_id="u9", revision=1)))
    assert result["step"] == "profile" and "restore_hint" not in result
    progress = (state.parent / "PROGRESS.md").read_text(encoding="utf-8")
    assert "## Refuted Directions\n\n- avoid e1 when {\"soc\": \"910B\"}" in progress


def test_subtarget_gain_becomes_baseline_and_waits_for_distill(state, repo):
    loop.complete(state, "profile", dict(bottleneck_summary=dict(op="a"), profile_ref="p"))
    loop.complete(state, "consult", dict(mode="reuse", unit_refs=[],
                                         skipped_reason="knowledge flywheel not enabled"))
    (repo / "kernel.py").write_text("x = 2\n", encoding="utf-8")
    loop.complete(state, "propose", dict(exp_id="e1", hypothesis="h", candidate_repo=str(repo)))
    loop.complete(state, "measure", measured(candidate=trials(103.0, 103.0)))
    assert loop.next_step(state)["verdict"] == "subtarget_valid"  # 1.03x under 1.05x
    result, _ = loop.complete(state, "learn", {})
    assert result["pending_distill"] == [dict(round=1, exp_id="e1", reason="not distilled yet")]
    assert result["step"] == "profile"
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["baseline"]["exp_id"] == "e1"
    record = ledger_of(state)[0]
    assert record["verdict"] == "subtarget_valid"
    assert record["knowledge_skipped"] == "knowledge flywheel not enabled"


def test_supported_without_distill_is_pending_with_note(state, repo):
    to_measure(state, repo, "x = 2\n")
    loop.complete(state, "measure", measured())
    result, _ = loop.complete(state, "learn", dict(note="distill after final ablation"))
    assert result["pending_distill"][0]["reason"] == "distill after final ablation"


def test_multi_factor_candidate_needs_ablation_receipt(state, repo):
    to_measure(state, repo, "x = 2\n", factors=["fuse", "tile"])
    result, _ = loop.complete(state, "measure", measured())
    assert result["verdict"] == "inconclusive" and result["entry"]["outcome"] == "ablation_pending"
    loop.complete(state, "learn", {})
    git(repo, "checkout", "--", "kernel.py")
    to_measure(state, repo, "x = 2\n", exp_id="e2", factors=["fuse", "tile"])
    result, _ = loop.complete(state, "measure", measured(
        run_ids=["r3", "r4"], ablation_receipt=dict(ablation_retained=True)))
    assert result["verdict"] == "supported"


def test_max_rounds_finishes(tmp_path, repo):
    path = tmp_path / "m" / "rsi-loop.json"
    loop.init(path, task_id="t1", ledger_path=tmp_path / "m" / "ledger.jsonl",
              baseline_repo=repo, direction="maximize", repetitions=2, max_rounds=1)
    to_measure(path, repo, "x = 2\n")
    loop.complete(path, "measure", measured())
    result, _ = loop.complete(path, "learn", dict(distill=dict(ok=True)))
    assert result["status"] == "finished" and "max_rounds" in result["finish_reason"]
    assert (tmp_path / "m" / "PROGRESS.md").is_file()


def test_cli_exit_codes(tmp_path, repo):
    path = tmp_path / "c" / "rsi-loop.json"
    script = str(SCRIPTS / "rsi_loop.py")
    args = ["--state", str(path)]

    def cli(*argv, body=None):
        done = subprocess.run([sys.executable, "-I", script, *argv],
                              input=None if body is None else json.dumps(body),
                              capture_output=True, text=True, timeout=60)
        return done.returncode, json.loads(done.stdout)

    code, out = cli("init", *args, "--task-id", "t1", "--ledger", str(tmp_path / "c" / "l.jsonl"),
                    "--baseline-repo", str(repo), "--direction", "maximize", "--repetitions", "2",
                    "--goal", "Speed up decode")
    assert code == 0 and out["step"] == "profile"
    assert cli("next", *args)[1]["round"] == 1
    code, out = cli("complete", "measure", *args, body=measured())
    assert code == 1 and out["status"] == "error" and "out of order" in out["error"]
    cli("complete", "profile", *args, body=dict(bottleneck_summary=dict(op="a"), profile_ref="p"))
    cli("complete", "consult", *args, body=dict(mode="reuse", unit_refs=[]))
    (repo / "kernel.py").write_text("x = 2\n", encoding="utf-8")
    propose = dict(exp_id="e1", hypothesis="h", candidate_repo=str(repo))
    assert cli("complete", "propose", *args, body=propose)[0] == 0
    cli("complete", "measure", *args, body=measured(candidate=trials(90.0, 91.0)))
    assert cli("complete", "learn", *args, body={})[0] == 0
    cli("complete", "consult", *args, body=dict(mode="reuse", unit_refs=[]))  # profile reused
    code, out = cli("complete", "propose", *args, body=dict(propose, exp_id="e2"))
    assert code == loop.BLOCKED and out["gate"]["decision"] == "duplicate"
    assert "Speed up decode" in (tmp_path / "c" / "PROGRESS.md").read_text(encoding="utf-8")
    assert cli("finish", *args, "--reason", "user stopped")[1]["status"] == "finished"
