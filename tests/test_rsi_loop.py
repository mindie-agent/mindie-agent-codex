"""The RSI loop advances one checked step at a time, resumes, gates and reports."""
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


def trials(*values, status="completed"):
    return [dict(status=status, metric=v) for v in values]


def to_measure(path, repo, change, exp_id="e1"):
    loop.complete(path, "profile", dict(bottleneck_summary=dict(op="rmsnorm", share=0.31),
                                        profile_ref="profile/r0"))
    mode = loop.next_step(path)["mode"]
    loop.complete(path, "consult", dict(mode=mode, unit_refs=[dict(unit_id="u1", revision=2)]))
    (repo / "kernel.py").write_text(change, encoding="utf-8")
    return loop.complete(path, "propose", dict(exp_id=exp_id, hypothesis="fuse rmsnorm",
                                               candidate_repo=str(repo),
                                               scope_conditions=dict(soc="910B")))


def measured(**changes):
    body = dict(run_ids=["r1", "r2"], baseline=trials(100.0, 100.0),
                candidate=trials(120.0, 118.0), correctness="passed", card_seconds=40)
    body.update(changes)
    return body


def test_supported_round_records_reports_and_moves_baseline(state, repo):
    result, code = to_measure(state, repo, "x = 2\n")
    assert code == 0 and result["step"] == "measure"
    result, _ = loop.complete(state, "measure", measured())
    assert result["step"] == "learn" and result["judgment"] == "supported"
    with pytest.raises(ValueError, match="knowledge_distill receipt"):
        loop.complete(state, "learn", {})
    result, _ = loop.complete(state, "learn", dict(distill=dict(unit_id="u9", revision=1)))
    assert result["round"] == 2 and result["step"] == "profile"
    records = ledger.read_ledger(state.parent / "experiment-ledger.jsonl")
    assert [r["judgment"] for r in records] == ["supported"]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["baseline"]["exp_id"] == "e1"
    report = Path(result["report"]).read_text(encoding="utf-8")
    assert "Judgment: supported" in report and "rmsnorm" in report
    assert "| 1 | 100.0 | 120.0 | 1.2 |" in report
    assert (state.parent / "rsi-reports" / "index.md").is_file()


def test_out_of_order_step_is_rejected_and_resume_points_to_current(state):
    with pytest.raises(ValueError, match="out of order"):
        loop.complete(state, "measure", measured())
    assert loop.next_step(state)["step"] == "profile"
    loop.complete(state, "profile", dict(bottleneck_summary=dict(op="a"), profile_ref="p"))
    resumed = loop.next_step(state)
    assert resumed["step"] == "consult" and "knowledge_query" in resumed["guide"]


def test_duplicate_source_is_not_submitted_again(state, repo):
    to_measure(state, repo, "x = 2\n")
    loop.complete(state, "measure", measured(candidate=trials(101.0, 100.0)))
    loop.complete(state, "learn", dict(conditional_avoid="no gain on 910B bf16"))
    # Same code again in round 2: the gate blocks it before the device.
    result, code = to_measure(state, repo, "x = 2\n", exp_id="e2")
    assert code == loop.BLOCKED and result["gate"]["decision"] == "duplicate"
    assert result["step"] == "propose" and result["skipped_this_round"][0]["exp_id"] == "e2"


def test_budget_exhausted_finishes_the_loop(tmp_path, repo):
    path = tmp_path / "b" / "rsi-loop.json"
    loop.init(path, task_id="t1", ledger_path=tmp_path / "b" / "ledger.jsonl",
              baseline_repo=repo, direction="maximize", repetitions=2, max_runs=2)
    to_measure(path, repo, "x = 2\n")
    loop.complete(path, "measure", measured(candidate=trials(101.0, 100.0)))
    loop.complete(path, "learn", dict(conditional_avoid="no gain"))
    result, code = to_measure(path, repo, "x = 3\n", exp_id="e2")
    assert code == loop.BLOCKED and result["status"] == "finished"
    assert result["finish_reason"].startswith("budget_exhausted")


def test_code_changed_after_gate_is_not_judged(state, repo):
    to_measure(state, repo, "x = 2\n")
    (repo / "kernel.py").write_text("x = 5\n", encoding="utf-8")
    result, code = loop.complete(state, "measure", measured())
    assert code == 1 and "changed after the gate" in result["error"]
    assert result["step"] == "propose"
    assert ledger.read_ledger(state.parent / "experiment-ledger.jsonl") == []


def test_infra_failure_is_inconclusive_and_needs_repair_note(state, repo):
    to_measure(state, repo, "x = 2\n")
    result, _ = loop.complete(state, "measure", measured(run_status="infra_failed"))
    assert result["judgment"] == "inconclusive"
    assert result["verdict"]["reasons"][0].startswith("run_status")
    with pytest.raises(ValueError, match="repair"):
        loop.complete(state, "learn", dict(conditional_avoid="x"))
    loop.complete(state, "learn", dict(repair="ssh dropped mid-run; rerun"))
    record = ledger.read_ledger(state.parent / "experiment-ledger.jsonl")[0]
    assert record["judgment"] == "inconclusive" and record["run_status"] == "infra_failed"


def test_three_stalled_rounds_switch_next_round_to_explore(tmp_path, repo):
    path = tmp_path / "s" / "rsi-loop.json"
    loop.init(path, task_id="t1", ledger_path=tmp_path / "s" / "ledger.jsonl",
              baseline_repo=repo, direction="maximize", repetitions=2)
    for index in range(3):
        assert loop.next_step(path)["mode"] == "reuse"
        to_measure(path, repo, f"x = {index + 10}\n", exp_id=f"e{index}")
        loop.complete(path, "measure", measured(run_ids=[f"r{index}"],
                                                candidate=trials(100.5, 100.0)))
        loop.complete(path, "learn", dict(conditional_avoid="no gain"))
    assert loop.next_step(path)["mode"] == "explore"
    loop.complete(path, "profile", dict(bottleneck_summary=dict(op="a"), profile_ref="p"))
    assert "subagent_fact_pack" in loop.next_step(path)["guide"]
    with pytest.raises(ValueError, match="explore"):
        loop.complete(path, "consult", dict(mode="reuse", unit_refs=[]))


def test_max_rounds_finishes_with_index(tmp_path, repo):
    path = tmp_path / "m" / "rsi-loop.json"
    loop.init(path, task_id="t1", ledger_path=tmp_path / "m" / "ledger.jsonl",
              baseline_repo=repo, direction="maximize", repetitions=2, max_rounds=1)
    to_measure(path, repo, "x = 2\n")
    loop.complete(path, "measure", measured())
    result, _ = loop.complete(path, "learn", dict(distill=dict(ok=True)))
    assert result["status"] == "finished" and "max_rounds" in result["finish_reason"]
    index = (tmp_path / "m" / "rsi-reports" / "index.md").read_text(encoding="utf-8")
    assert "| 1 | `e1` | supported |" in index


def test_cli_resume_and_blocked_exit_code(tmp_path, repo, capsys):
    path = tmp_path / "c" / "rsi-loop.json"
    args = ["--state", str(path)]
    assert loop.main(["init", *args, "--task-id", "t1", "--ledger",
                      str(tmp_path / "c" / "l.jsonl"), "--baseline-repo", str(repo),
                      "--direction", "maximize", "--repetitions", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["step"] == "profile"
    assert loop.main(["next", *args]) == 0
    assert json.loads(capsys.readouterr().out)["round"] == 1
    assert loop.main(["finish", *args, "--reason", "user stopped"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "finished"
