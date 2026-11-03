"""Fingerprint, raw-trial check and pre-device gate around the MindIE Bench RSI ledger."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import experiment_ledger as ledger  # noqa: E402


def trials(*values, status="completed"):
    return [dict(status=status, metric=v) for v in values]


def evidence(**changes):
    body = dict(repetitions=2, direction="maximize", target_speedup=1.05,
                baseline=trials(100.0, 100.0), candidate=trials(120.0, 118.0),
                correctness="passed", baseline_source_digest="sha256:a",
                source_digest="sha256:b", run_ids=["r1", "r2"], exp_id="e1",
                hypothesis="fuse rmsnorm", scope_conditions=dict(soc="910B"))
    body.update(changes)
    return body


def record(**changes):
    """A ledger record as rsi_loop writes it: the bench entry plus our fields."""
    run_ids = changes.pop("run_ids", ["r1"])
    verdict = changes.pop("verdict", "supported")
    scope = changes.pop("conditions", dict(soc="910B"))
    entry = dict(run_id=",".join(run_ids), candidate_id=changes.get("exp_id", "e1"),
                 mutation_summary="fuse rmsnorm", verdict=verdict, conditions=scope,
                 failure_reason="")
    entry.update(task_id="t1", exp_id="e1", run_ids=run_ids, source_digest="sha256:b",
                 run_status="completed", card_seconds=60)
    entry.update(changes)
    return entry


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   timeout=15)


def test_clean_speedup_is_supported_by_bench_gates():
    result = ledger.judge(evidence())
    assert result["verdict"] == "supported" and result["outcome"] == "verified", result
    assert result["speedup_ratio"] == pytest.approx(1.19)
    assert result["gates"]["gate4_distill_or_refute"]["passed"] is True
    assert result["run_id"] == "r1,r2" and result["candidate_id"] == "e1"


def test_gain_below_target_is_subtarget_valid():
    result = ledger.judge(evidence(candidate=trials(103.0, 103.0)))
    assert result["verdict"] == "subtarget_valid"
    assert result["axes"]["goal_attainment"] == "not_attained"


def test_ratio_inside_noise_is_inconclusive():
    result = ledger.judge(evidence(candidate=trials(100.5, 100.0)))
    assert result["verdict"] == "inconclusive" and result["outcome"] == "speedup_within_noise"


def test_regression_beyond_noise_is_refuted():
    result = ledger.judge(evidence(candidate=trials(90.0, 92.0)))
    assert result["verdict"] == "refuted"
    assert result["conditional_avoid"] and result["conditional_avoid"][0]["conditions"] == \
        dict(soc="910B")


def test_correctness_failure_is_refuted():
    result = ledger.judge(evidence(correctness="failed"))
    assert result["verdict"] == "refuted" and result["outcome"] == "oracle_failed"


def test_multi_factor_change_without_ablation_is_pending():
    result = ledger.judge(evidence(factors=["fuse", "tile"]))
    assert result["verdict"] == "inconclusive" and result["outcome"] == "ablation_pending"
    receipt = ledger.judge(evidence(factors=["fuse", "tile"],
                                    ablation_receipt=dict(ablation_retained=True)))
    assert receipt["verdict"] == "supported"


@pytest.mark.parametrize("changes, problem", [
    (dict(candidate=trials(120.0) + trials(None, status="infra_failed")), "infra_failed"),
    (dict(candidate=trials(120.0)), "expected 2 trials"),
    (dict(candidate=trials(120.0, -1.0)), "not positive"),
    (dict(correctness=None), "correctness was not verified"),
    (dict(source_digest=None), "fingerprint missing"),
    (dict(source_digest="sha256:a"), "equals baseline"),
    (dict(run_status="timeout"), "run_status 'timeout'"),
    (dict(compile_ok=False), "did not build"),
])
def test_raw_trial_problems_make_the_bench_verdict_inconclusive(changes, problem):
    result = ledger.judge(evidence(**changes))
    assert result["verdict"] == "inconclusive", result
    assert any(problem in p for p in result["evidence_problems"])


def test_missing_evidence_outranks_a_failed_correctness_check():
    result = ledger.judge(evidence(correctness="failed",
                                   candidate=trials(120.0) + trials(None, status="infra_failed")))
    assert result["verdict"] == "inconclusive"


def test_minimize_direction_inverts_ratio():
    result = ledger.judge(evidence(direction="minimize", baseline=trials(10.0, 10.0),
                                   candidate=trials(8.0, 8.0)))
    assert result["verdict"] == "supported"
    assert result["speedup_ratio"] == pytest.approx(1.25)


def test_append_writes_bench_ledger_and_progress(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    entry = dict(ledger.judge(evidence()), task_id="t1", exp_id="e1", run_ids=["r1", "r2"],
                 source_digest="sha256:b", run_status="completed")
    result = ledger.append(path, entry, goal="Speed up decode")
    progress = (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")
    assert result["progress"] == str(tmp_path / "PROGRESS.md")
    assert progress.startswith("# RSI Progress: t1") and "Speed up decode" in progress
    assert "- candidate: `e1`" in progress and "fuse rmsnorm" in progress
    assert ledger.read_ledger(path)[0]["schema_version"] == entry["schema_version"]


def test_append_rejects_infra_failure_not_inconclusive_and_silent_rewrite(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    with pytest.raises(ValueError, match="inconclusive"):
        ledger.append(path, record(run_status="infra_failed", verdict="refuted"))
    with pytest.raises(ValueError, match="source_digest"):
        ledger.append(path, record(source_digest=""))
    assert not path.exists()
    ledger.append(path, record())
    with pytest.raises(ValueError, match="supersedes_exp_id"):
        ledger.append(path, record(verdict="refuted"))
    ledger.append(path, record(verdict="refuted", supersedes_exp_id="e1"))
    raw = path.read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw


def test_gate_blocks_settled_duplicate_but_retries_inconclusive(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(exp_id="e0", source_digest="sha256:x",
                               verdict="inconclusive", run_status="infra_failed"))
    assert ledger.gate(path, task_id="t1", source_digest="sha256:x",
                       scope=dict(soc="910B"))["decision"] == "submit"
    ledger.append(path, record(verdict="subtarget_valid"))
    dup = ledger.gate(path, task_id="t1", source_digest="sha256:b", scope=dict(soc="910B"))
    assert dup["decision"] == "duplicate" and dup["prior"]["exp_id"] == "e1"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:b",
                       scope=dict(soc="910C"))["decision"] == "submit"
    assert ledger.gate(path, task_id="t2", source_digest="sha256:b",
                       scope=dict(soc="910B"))["decision"] == "submit"


def test_missing_scope_matches_only_an_unscoped_record(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record())  # scoped to 910B
    assert ledger.gate(path, task_id="t1", source_digest="sha256:b")["decision"] == "submit"
    ledger.append(path, record(exp_id="e2", source_digest="sha256:c", conditions={}))
    assert ledger.gate(path, task_id="t1", source_digest="sha256:c",
                       scope={})["decision"] == "duplicate"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:c")["decision"] == "duplicate"


def test_gate_enforces_run_and_card_second_budget(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(run_ids=["r1", "r2"], card_seconds=90))
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new",
                       max_runs=2)["decision"] == "budget_exhausted"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new",
                       max_card_seconds=90)["decision"] == "budget_exhausted"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new", max_runs=3,
                       max_card_seconds=120)["decision"] == "submit"


def test_superseding_revision_is_not_charged_twice(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(run_ids=["r1", "r2"], card_seconds=60))
    # Same runs, corrected verdict and card time: replaces, does not add.
    ledger.append(path, record(run_ids=["r1", "r2"], card_seconds=50, verdict="refuted",
                               supersedes_exp_id="e1"))
    spent = ledger.gate(path, task_id="t1", source_digest="sha256:z")["spent"]
    assert spent["runs"] == 2 and spent["card_seconds"] == 50
    # A revision that adds a rerun adds its time.
    ledger.append(path, record(run_ids=["r1", "r2", "r3"], card_seconds=30,
                               supersedes_exp_id="e1"))
    spent = ledger.gate(path, task_id="t1", source_digest="sha256:z")["spent"]
    assert spent["runs"] == 3 and spent["card_seconds"] == 80
    live, _ = ledger.fold(ledger.read_ledger(path))
    assert len(live) == 1 and live[0]["run_ids"] == ["r1", "r2", "r3"]
    assert ledger.gate(path, task_id="t1", source_digest="sha256:b",
                       scope=dict(soc="910B"))["prior"]["verdict"] == "supported"


def test_fingerprint_changes_with_tracked_and_untracked_edits(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "base")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    first = ledger.fingerprint(repo)
    assert first["dirty"] is True and first["untracked_files"] == 1
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8", newline="\n")
    second = ledger.fingerprint(repo)
    assert second["source_digest"] != first["source_digest"]
    assert ledger.fingerprint(repo)["source_digest"] == second["source_digest"]
    (repo / "pkg").mkdir()
    with pytest.raises(ValueError, match="root"):
        ledger.fingerprint(repo / "pkg")


def test_large_untracked_file_is_identified_by_size(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    (repo / "dump.bin").write_bytes(b"0" * 64)
    monkeypatch.setattr(ledger, "LARGE_FILE_BYTES", 16)
    first = ledger.fingerprint(repo)
    assert first["unhashed_large_files"] == ["dump.bin"]
    (repo / "dump.bin").write_bytes(b"1" * 64)  # same size: same identity
    assert ledger.fingerprint(repo)["source_digest"] == first["source_digest"]
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert ledger.fingerprint(repo)["source_digest"] != first["source_digest"]


def test_cli_exit_codes_distinguish_blocked_from_error(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    script = str(SCRIPTS / "experiment_ledger.py")
    run = dict(capture_output=True, text=True, timeout=30)
    judged = subprocess.run([sys.executable, "-I", script, "judge"],
                            input=json.dumps(evidence()), **run)
    assert judged.returncode == 0, judged.stderr
    entry = dict(json.loads(judged.stdout), task_id="t1", exp_id="e1", run_ids=["r1", "r2"],
                 source_digest="sha256:b", run_status="completed")
    appended = subprocess.run([sys.executable, "-I", script, "append", "--ledger", str(path)],
                              input=json.dumps(entry), **run)
    assert appended.returncode == 0, appended.stderr
    assert (tmp_path / "PROGRESS.md").is_file()
    blocked = subprocess.run([sys.executable, "-I", script, "gate", "--ledger", str(path),
                              "--task-id", "t1", "--source-digest", "sha256:b",
                              "--scope", json.dumps(dict(soc="910B"))], **run)
    assert blocked.returncode == ledger.BLOCKED
    assert json.loads(blocked.stdout)["decision"] == "duplicate"
    broken = subprocess.run([sys.executable, "-I", script, "judge"], input="{}", **run)
    assert broken.returncode == 1 and json.loads(broken.stdout)["status"] == "error"
