"""Experiment ledger judgment, source fingerprint, dedup and budget are code-enforced."""
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
                source_digest="sha256:b")
    body.update(changes)
    return body


def record(**changes):
    body = dict(task_id="t1", exp_id="e1", hypothesis="fuse rmsnorm", run_ids=["r1"],
                source_digest="sha256:b", judgment="supported", run_status="completed",
                scope_conditions=dict(soc="910B"), card_seconds=60)
    body.update(changes)
    return body


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   timeout=15)


def test_complete_gain_is_supported_by_engine_gate():
    result = ledger.judge(evidence())
    assert result["judgment"] == "supported", result
    assert result["engine_verdict"] in ("distill_candidate", "retain_verified_evidence")
    assert result["speedup"] == pytest.approx(1.19)


def test_gain_below_target_is_refuted():
    result = ledger.judge(evidence(candidate=trials(101.0, 102.0)))
    assert result["judgment"] == "refuted", result


def test_missing_evidence_outranks_a_failed_gate():
    # Correctness failed AND a trial crashed: missing evidence wins.
    result = ledger.judge(evidence(correctness="failed",
                                   candidate=trials(120.0) + trials(None, status="infra_failed")))
    assert result["judgment"] == "inconclusive"
    assert any("infra_failed" in r for r in result["reasons"])


@pytest.mark.parametrize("changes", [
    dict(compile_ok=False),
    dict(correctness=None),
    dict(source_digest=None),
    dict(source_digest="sha256:a"),
    dict(candidate=trials(120.0)),
])
def test_incomplete_evidence_is_inconclusive_not_refuted(changes):
    assert ledger.judge(evidence(**changes))["judgment"] == "inconclusive"


def test_minimize_direction_inverts_ratio():
    result = ledger.judge(evidence(direction="minimize", baseline=trials(10.0, 10.0),
                                   candidate=trials(8.0, 8.0)))
    assert result["judgment"] == "supported"
    assert result["speedup"] == pytest.approx(1.25)


def test_append_rejects_infra_failure_recorded_as_refuted(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    with pytest.raises(ValueError, match="inconclusive"):
        ledger.append(path, record(run_status="infra_failed", judgment="refuted"))
    with pytest.raises(ValueError, match="source_digest"):
        ledger.append(path, record(source_digest=""))
    assert not path.exists()


def test_append_is_one_lf_line_and_rejects_silent_rewrite(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record())
    with pytest.raises(ValueError, match="supersedes_exp_id"):
        ledger.append(path, record(judgment="refuted"))
    ledger.append(path, record(exp_id="e2", supersedes_exp_id="e1", judgment="refuted"))
    raw = path.read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw
    assert [r["schema"] for r in ledger.read_ledger(path)] == [ledger.SCHEMA] * 2


def test_gate_blocks_duplicate_source_but_retries_inconclusive(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(exp_id="e0", source_digest="sha256:x",
                               judgment="inconclusive", run_status="infra_failed"))
    assert ledger.gate(path, task_id="t1", source_digest="sha256:x")["decision"] == "submit"
    ledger.append(path, record())
    dup = ledger.gate(path, task_id="t1", source_digest="sha256:b", scope=dict(soc="910B"))
    assert dup["decision"] == "duplicate" and dup["prior"]["exp_id"] == "e1"
    other_scope = ledger.gate(path, task_id="t1", source_digest="sha256:b",
                              scope=dict(soc="910C"))
    assert other_scope["decision"] == "submit"
    assert ledger.gate(path, task_id="t2", source_digest="sha256:b")["decision"] == "submit"


def test_gate_enforces_run_and_card_second_budget(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(run_ids=["r1", "r2"], card_seconds=90))
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new",
                       max_runs=2)["decision"] == "budget_exhausted"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new",
                       max_card_seconds=90)["decision"] == "budget_exhausted"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new", max_runs=3,
                       max_card_seconds=120)["decision"] == "submit"


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


def test_cli_exit_codes_distinguish_blocked_from_error(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    script = str(SCRIPTS / "experiment_ledger.py")
    appended = subprocess.run([sys.executable, "-I", script, "append", "--ledger", str(path)],
                              input=json.dumps(record()), capture_output=True, text=True,
                              timeout=5)
    assert appended.returncode == 0, appended.stderr
    blocked = subprocess.run([sys.executable, "-I", script, "gate", "--ledger", str(path),
                              "--task-id", "t1", "--source-digest", "sha256:b"],
                             capture_output=True, text=True, timeout=5)
    assert blocked.returncode == ledger.BLOCKED
    assert json.loads(blocked.stdout)["decision"] == "duplicate"
    broken = subprocess.run([sys.executable, "-I", script, "judge"], input="{}",
                            capture_output=True, text=True, timeout=5)
    assert broken.returncode == 1 and json.loads(broken.stdout)["status"] == "error"
