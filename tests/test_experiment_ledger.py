"""Fingerprint, raw-trial check, 4-gate RSI judgment, and pre-device gate for the RSI ledger."""
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
    """A ledger record as rsi_loop writes it: the judged entry plus our fields."""
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


def test_clean_speedup_is_supported_by_rsi_gates():
    result = ledger.judge(evidence())
    assert result["verdict"] == "supported" and result["outcome"] == "verified", result
    assert result["speedup_ratio"] == pytest.approx(1.19)
    assert result["baseline_metric"] == pytest.approx(100.0)
    assert result["candidate_metric"] == pytest.approx(119.0)
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


def test_multi_factor_change_without_ablation_is_pending_even_below_target():
    # P2 #2: Both above-target (1.19x) and sub-target (1.03x < 1.05x) multi-factor
    # gains without an ablation receipt must stay inconclusive (ablation_pending).
    above = ledger.judge(evidence(factors=["fuse", "tile"]))
    assert above["verdict"] == "inconclusive" and above["outcome"] == "ablation_pending"
    assert above["gate_reached"] == 3
    subtarget = ledger.judge(evidence(factors=["fuse", "tile"],
                                      candidate=trials(103.0, 103.0)))
    assert subtarget["verdict"] == "inconclusive" and subtarget["outcome"] == "ablation_pending"
    assert subtarget["gate_reached"] == 3
    # Contract C4: candidate_for_sequential_pruning with pruned_combination_verified=False -> RETAIN
    # even when a raw LOO field tentatively set ablation_retained=False.
    c4_retained = ledger.judge(evidence(
        factors=["fuse", "tile"],
        candidate=trials(103.0, 103.0),
        ablation_receipt=dict(status="candidate_for_sequential_pruning",
                              pruned_combination_verified=False,
                              ablation_retained=False),
    ))
    assert c4_retained["verdict"] == "subtarget_valid"
    # A failed ablation receipt with pruned_combination_verified=False must NOT be treated as retained.
    failed_ablation = ledger.judge(evidence(
        factors=["fuse", "tile"],
        ablation_receipt=dict(status="failed", pruned_combination_verified=False),
    ))
    assert failed_ablation["verdict"] == "inconclusive" and failed_ablation["outcome"] == "ablation_pending"
    # Non-boolean ablation flags are rejected.
    with pytest.raises(ValueError, match="boolean"):
        ledger.judge(evidence(factors=["fuse", "tile"],
                              ablation_receipt=dict(ablation_retained="false")))
    with pytest.raises(ValueError, match="boolean"):
        ledger.judge(evidence(factors=["fuse", "tile"], ablation_retained=1))
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
def test_raw_trial_problems_make_the_verdict_inconclusive(changes, problem):
    result = ledger.judge(evidence(**changes))
    assert result["verdict"] == "inconclusive", result
    assert any(problem in p for p in result["evidence_problems"])


def test_missing_evidence_outranks_failed_correctness_and_hidden_input_checks():
    # P2 #1: Missing trial (1 of 2) combined with correctness="failed" AND
    # hidden_input_summary={"generalization_ok": False} must remain inconclusive.
    result = ledger.judge(evidence(
        correctness="failed",
        candidate=trials(120.0),
        hidden_input_summary=dict(generalization_ok=False),
    ))
    assert result["verdict"] == "inconclusive"
    assert result["outcome"] == "measurement_invalid"
    assert result["gate_reached"] == 2
    assert result["conditional_avoid"] == []

    # Direct gate call with measurement_passed=False and oracle_passed=False must also be inconclusive.
    direct = ledger.evaluate_rsi_candidate_gates(
        run_id="r1",
        candidate_id="e1",
        mutation_summary="fuse rmsnorm",
        compile_ok=True,
        oracle_passed=False,
        measurement_passed=False,
        speedup_ratio=1.2,
        hidden_input_summary=dict(generalization_ok=False),
    )
    assert direct["verdict"] == "inconclusive"
    assert direct["outcome"] == "measurement_invalid"
    assert direct["gate_reached"] == 2
    assert direct["gates"]["gate1_compile_oracle"]["oracle_verified"] is False


def test_non_finite_speedup_ratios_are_inconclusive_measurement_invalid():
    for bad_ratio in (float("nan"), float("inf"), float("-inf"), -1.0, 0.0):
        res = ledger.evaluate_rsi_candidate_gates(
            run_id="r1", candidate_id="e1", mutation_summary="overflow",
            compile_ok=True, oracle_passed=True, measurement_passed=True,
            speedup_ratio=bad_ratio,
        )
        assert res["verdict"] == "inconclusive" and res["outcome"] == "measurement_invalid"
        assert res["gate_reached"] == 2 and res["speedup_ratio"] is None
    overflow = ledger.judge(evidence(
        baseline=trials(1e-308, 1e-308),
        candidate=trials(1e308, 1e308),
    ))
    assert overflow["verdict"] == "inconclusive" and overflow["outcome"] == "measurement_invalid"


def test_minimize_direction_inverts_ratio():
    result = ledger.judge(evidence(direction="minimize", baseline=trials(10.0, 10.0),
                                   candidate=trials(8.0, 8.0)))
    assert result["verdict"] == "supported"
    assert result["speedup_ratio"] == pytest.approx(1.25)


def test_append_writes_ledger_and_progress_without_mindie_bench(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mindie_bench", None)
    path = tmp_path / "experiment-ledger.jsonl"
    entry = dict(ledger.judge(evidence()), task_id="t1", exp_id="e1", run_ids=["r1", "r2"],
                 source_digest="sha256:b", run_status="completed")
    result = ledger.append(path, entry, goal="Speed up decode")
    progress = (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")
    assert result["progress"] == str(tmp_path / "PROGRESS.md")
    assert progress.startswith("# RSI Progress: t1") and "Speed up decode" in progress
    assert "- candidate: `e1`" in progress and "fuse rmsnorm" in progress
    assert ledger.read_ledger(path)[0]["schema_version"] == ledger.SCHEMA_VERSION


def test_append_preserves_in_flight_runs_and_recovery_sections_in_progress(tmp_path):
    # P1: Pre-existing in_flight_runs, endpoint, slot_id, and manifest in PROGRESS.md
    # must be preserved across multiple append() updates.
    path = tmp_path / "experiment-ledger.jsonl"
    progress_file = tmp_path / "PROGRESS.md"
    initial_recovery = (
        "# RSI Progress: t1\n\n"
        "in_flight_runs:\n"
        "- run_id: exec-901\n"
        "  slot_id: npu-slot-0\n"
        "  endpoint: ssh://173.125.1.2:22\n"
        "  manifest: /tmp/manifests/exec-901.json\n\n"
        "## in_flight_runs\n\n"
        "- `job-77` on `192.168.13.165` (`slot_id=die0`, `manifest=/var/run/m77.json`)\n"
    )
    progress_file.write_text(initial_recovery, encoding="utf-8", newline="\n")

    e1 = dict(ledger.judge(evidence()), task_id="t1", exp_id="e1", run_ids=["r1", "r2"],
              baseline_source_digest="sha256:a", source_digest="sha256:b",
              run_status="completed")
    ledger.append(path, e1, progress=progress_file, goal="Speed up decode")

    e2 = dict(ledger.judge(evidence(exp_id="e2", hypothesis="bad tile",
                                    candidate=trials(90.0, 91.0),
                                    baseline_source_digest="sha256:b",
                                    source_digest="sha256:c", run_ids=["r3", "r4"])),
              task_id="t1", exp_id="e2", run_ids=["r3", "r4"],
              baseline_source_digest="sha256:b", source_digest="sha256:c",
              run_status="completed")
    ledger.append(path, e2, progress=progress_file, goal="Speed up decode")

    rendered = progress_file.read_text(encoding="utf-8")
    assert "exec-901" in rendered
    assert "ssh://173.125.1.2:22" in rendered
    assert "/tmp/manifests/exec-901.json" in rendered
    assert "job-77" in rendered and "192.168.13.165" in rendered
    assert rendered.count("run_id: exec-901") == 1
    assert rendered.count("job-77") == 1
    assert "- candidate: `e1`" in rendered
    assert "avoid e2 when {\"soc\": \"910B\"}" in rendered

    # Even if a recovery section is inserted inside the marker block, or plain recovery
    # lines appear inside a legacy managed section, re-rendering preserves them.
    inside_markers = rendered.replace(
        ledger.PROGRESS_MARKER_END,
        "## in_flight_runs\n\n- `job-88` (`slot_id=die1`, `endpoint=ssh://a3`)\n"
        + ledger.PROGRESS_MARKER_END,
    )
    progress_file.write_text(inside_markers, encoding="utf-8", newline="\n")
    ledger.render_progress_file(path, progress=progress_file, task_id="t1", goal="Speed up decode")
    after_inner = progress_file.read_text(encoding="utf-8")
    assert "job-88" in after_inner and "ssh://a3" in after_inner

    legacy_goal_recovery = (
        "# RSI Progress: t1\n\n"
        "## Goal\n\n"
        "Speed up decode\n"
        "- `job-88` (`slot_id=die1`, `endpoint=ssh://a3`, `manifest=/tmp/m.json`)\n\n"
        "{\"in_flight_runs\": [{\"job_id\": \"job-json-9\", \"endpoint\": \"ssh://a2\"}]}\n\n"
        "in_flight_runs:\n"
        "- run_id: exec-legacy-55\n"
        "  slot_id: npu-slot-3\n"
        "  endpoint: ssh://192.168.13.154:22\n"
        "  manifest: /tmp/m55.json\n"
    )
    progress_file.write_text(legacy_goal_recovery, encoding="utf-8", newline="\n")
    ledger.render_progress_file(path, progress=progress_file, task_id="t1", goal="Speed up decode")
    ledger.render_progress_file(path, progress=progress_file, task_id="t1", goal="Speed up decode")
    migrated = progress_file.read_text(encoding="utf-8")
    assert "exec-legacy-55" in migrated and "192.168.13.154" in migrated
    assert "job-88" in migrated and "slot_id=die1" in migrated
    assert "job-json-9" in migrated
    assert migrated.count("run_id: exec-legacy-55") == 1
    assert migrated.count("job-88") == 1


def test_progress_ranks_progressive_baselines_by_cumulative_gain_and_folds_supersedes(tmp_path):
    # P2 #3 + P1 fold: Round 1 improves 100 -> 120 (1.20x), Round 2 improves 120 -> 132 (1.10x).
    # Round 2 has cumulative gain 1.32x and candidate_metric 132 > 120, so e2 must be Best Candidate.
    path = tmp_path / "experiment-ledger.jsonl"
    e1 = dict(
        ledger.judge(evidence(
            exp_id="e1", hypothesis="vectorize load",
            baseline=trials(100.0, 100.0), candidate=trials(120.0, 120.0),
            baseline_source_digest="sha256:s0", source_digest="sha256:s1",
            run_ids=["r1", "r2"],
        )),
        task_id="t1", exp_id="e1", run_ids=["r1", "r2"],
        baseline_source_digest="sha256:s0", source_digest="sha256:s1",
        run_status="completed",
    )
    e2 = dict(
        ledger.judge(evidence(
            exp_id="e2", hypothesis="double buffer",
            baseline=trials(120.0, 120.0), candidate=trials(132.0, 132.0),
            baseline_source_digest="sha256:s1", source_digest="sha256:s2",
            run_ids=["r3", "r4"],
        )),
        task_id="t1", exp_id="e2", run_ids=["r3", "r4"],
        baseline_source_digest="sha256:s1", source_digest="sha256:s2",
        run_status="completed",
    )
    ledger.append(path, e1)
    ledger.append(path, e2)
    progress = (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")
    best_section = progress.split("## Best Supported Candidate\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert "- candidate: `e2` (`supported`, `1.1000x`, cumulative `1.3200x`) — double buffer" in best_section

    # If e2 is later superseded as refuted, fold() removes e2 from Best Supported Candidate and restores e1.
    e2_rev = dict(
        ledger.judge(evidence(
            exp_id="e2", hypothesis="double buffer",
            baseline=trials(120.0, 120.0), candidate=trials(132.0, 132.0),
            correctness="failed",
            baseline_source_digest="sha256:s1", source_digest="sha256:s2",
            run_ids=["r3", "r4"],
        )),
        task_id="t1", exp_id="e2", supersedes_exp_id="e2", run_ids=["r3", "r4"],
        baseline_source_digest="sha256:s1", source_digest="sha256:s2",
        run_status="completed",
    )
    ledger.append(path, e2_rev)
    progress_after = (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")
    best_after = progress_after.split("## Best Supported Candidate\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert "- candidate: `e1`" in best_after and "`e2`" not in best_after

    # A progressive subtarget_valid candidate (120 -> 123.6, 1.03x step, 1.236x cumulative)
    # that advances the e1 (1.20x) baseline must rank ahead of e1, even if e1 is later revised.
    e3 = dict(
        ledger.judge(evidence(
            exp_id="e3", hypothesis="tail peel",
            baseline=trials(120.0, 120.0), candidate=trials(123.6, 123.6),
            baseline_source_digest="sha256:s1", source_digest="sha256:s3",
            run_ids=["r5", "r6"],
        )),
        task_id="t1", exp_id="e3", run_ids=["r5", "r6"],
        baseline_source_digest="sha256:s1", source_digest="sha256:s3",
        run_status="completed",
    )
    e1_rev = dict(e1, card_seconds=45, supersedes_exp_id="e1")
    ledger.append(path, e3)
    ledger.append(path, e1_rev)
    progress_sub = (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")
    best_sub = progress_sub.split("## Best Supported Candidate\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert "- candidate: `e3` (`subtarget_valid`, `1.0300x`, cumulative `1.2360x`)" in best_sub

    # Unlinked records without baseline_source_digest must NOT be chained by list adjacency.
    unlinked_path = tmp_path / "unlinked.jsonl"
    ledger.append(unlinked_path, record(exp_id="u1", source_digest="sha256:u1", speedup_ratio=1.20))
    ledger.append(unlinked_path, record(exp_id="u2", source_digest="sha256:u2", speedup_ratio=1.10))
    unlinked_md = (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")
    assert "- candidate: `u1` (`supported`, `1.2000x`)" in unlinked_md
    assert "cumulative" not in unlinked_md

    # Explicit baseline_exp_id disambiguates duplicate source_digest independent of list order,
    # while cycles and ambiguous duplicate parent digests are rejected.
    r_a = record(exp_id="e1", source_digest="s1", baseline_source_digest="s0", speedup_ratio=1.2)
    r_b = record(exp_id="e2", source_digest="s2", baseline_source_digest="s1",
                 baseline_exp_id="e1", speedup_ratio=1.1)
    r_c = record(exp_id="e3", source_digest="s1", baseline_source_digest="s9", speedup_ratio=1.5)
    rank_abc = [(r["exp_id"], round(cum, 4)) for r, _, cum in ledger._rank_supported_candidates([r_a, r_b, r_c])]
    rank_cba = [(r["exp_id"], round(cum, 4)) for r, _, cum in ledger._rank_supported_candidates([r_c, r_b, r_a])]
    assert rank_abc == [("e3", 1.5), ("e2", 1.32), ("e1", 1.2)]
    assert rank_cba == rank_abc

    cyc1 = record(exp_id="c1", source_digest="cs1", baseline_source_digest="cs2", speedup_ratio=1.2)
    cyc2 = record(exp_id="c2", source_digest="cs2", baseline_source_digest="cs1", speedup_ratio=1.1)
    assert ledger._rank_supported_candidates([cyc1, cyc2]) == []
    assert ledger._rank_supported_candidates([cyc2, cyc1]) == []


def test_append_keeps_recorded_status_with_warning_if_progress_write_fails(tmp_path, monkeypatch):
    path = tmp_path / "experiment-ledger.jsonl"
    entry = dict(ledger.judge(evidence()), task_id="t1", exp_id="e1", run_ids=["r1", "r2"],
                 source_digest="sha256:b", run_status="completed")

    def broken_write(*args, **kwargs):
        raise OSError("disk full while writing PROGRESS.md")

    monkeypatch.setattr(ledger, "_write_progress_atomic", broken_write)
    res = ledger.append(path, entry, goal="Speed up decode")
    assert res["status"] == "recorded"
    assert res["progress_unconfirmed"] is True
    assert "disk full" in res["progress_warning"]
    assert len(ledger.read_ledger(path)) == 1

    monkeypatch.undo()
    rendered = ledger.render_progress_file(path, task_id="t1", goal="Speed up decode")
    assert rendered["status"] == "rendered"
    assert "- candidate: `e1`" in (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")

    # TimeoutError and corrupted PROGRESS.md (UnicodeDecodeError) after ledger commit
    # also keep status="recorded" with progress_unconfirmed=True.
    (tmp_path / "PROGRESS.md").write_bytes(b"\xff\xfe\x00invalid-utf8")
    e2 = dict(ledger.judge(evidence(exp_id="e2", source_digest="sha256:c", run_ids=["r3", "r4"])),
              task_id="t1", exp_id="e2", run_ids=["r3", "r4"],
              source_digest="sha256:c", run_status="completed")
    res_utf8 = ledger.append(path, e2, goal="Speed up decode")
    assert res_utf8["status"] == "recorded" and res_utf8["progress_unconfirmed"] is True

    def timeout_write(*args, **kwargs):
        raise TimeoutError("progress lock timed out")

    monkeypatch.setattr(ledger, "_write_progress_atomic", timeout_write)
    e3 = dict(ledger.judge(evidence(exp_id="e3", source_digest="sha256:d", run_ids=["r5", "r6"])),
              task_id="t1", exp_id="e3", run_ids=["r5", "r6"],
              source_digest="sha256:d", run_status="completed")
    res_timeout = ledger.append(path, e3, goal="Speed up decode")
    assert res_timeout["status"] == "recorded" and res_timeout["progress_unconfirmed"] is True
    assert "TimeoutError" in res_timeout["progress_warning"]


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


def test_progress_preserves_lead_in_fenced_recovery_json_inside_managed_section(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    progress_path = tmp_path / "PROGRESS.md"
    fenced_block = (
        "Recover this job:\n"
        "```json\n"
        "{\n"
        '  "run_id": "r-active-9",\n'
        '  "job_id": "j-active-9",\n'
        '  "slot_id": "s-active-9"\n'
        "}\n"
        "```"
    )
    progress_path.write_text(
        "# RSI Progress: t1\n\n"
        "## Goal\n\n"
        "Optimize decode.\n\n"
        f"{fenced_block}\n\n"
        "## Best Supported Candidate\n\n"
        "- None yet.\n",
        encoding="utf-8",
    )
    ledger.append(path, record(exp_id="e1", speedup_ratio=1.18))
    rendered = progress_path.read_text(encoding="utf-8")
    assert fenced_block in rendered
    assert '"run_id": "r-active-9"' in rendered


def test_lineage_rejects_refuted_digest_parent_and_resolves_superseded_alias(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    progress_path = tmp_path / "PROGRESS.md"
    # Case 1: e1 is refuted with source_digest="sha256:s1"; e2 links only by baseline_source_digest="sha256:s1".
    ledger.append(path, record(
        exp_id="e1", source_digest="sha256:s1", baseline_source_digest="sha256:s0",
        verdict="refuted", outcome="speedup_below_target", speedup_ratio=0.90,
    ))
    ledger.append(path, record(
        exp_id="e2", source_digest="sha256:s2", baseline_source_digest="sha256:s1",
        verdict="supported", outcome="verified", speedup_ratio=1.50,
    ))
    best_section = progress_path.read_text(encoding="utf-8").split(
        "## Best Supported Candidate\n\n", 1
    )[1].split("\n\n## ", 1)[0]
    assert "- None yet." in best_section

    # Case 2: e3 (1.20x) is renamed via supersedes_exp_id="e3" -> exp_id="e3-r2",
    # and e4 (1.10x) still references baseline_exp_id="e3".
    ledger.append(path, record(
        exp_id="e3", source_digest="sha256:s3", baseline_source_digest="sha256:s0",
        verdict="supported", outcome="verified", speedup_ratio=1.20,
    ))
    ledger.append(path, record(
        exp_id="e3-r2", supersedes_exp_id="e3", source_digest="sha256:s3",
        baseline_source_digest="sha256:s0", verdict="supported", outcome="verified",
        speedup_ratio=1.20,
    ))
    ledger.append(path, record(
        exp_id="e4", baseline_exp_id="e3", source_digest="sha256:s4",
        baseline_source_digest="sha256:s3", verdict="supported", outcome="verified",
        speedup_ratio=1.10,
    ))
    best_section_after = progress_path.read_text(encoding="utf-8").split(
        "## Best Supported Candidate\n\n", 1
    )[1].split("\n\n## ", 1)[0]
    assert "- candidate: `e4` (`supported`, `1.1000x`, cumulative `1.3200x`)" in best_section_after


def test_fold_preserves_accumulated_card_seconds_across_lesson_only_revision(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(exp_id="e1", run_ids=["r1", "r2"], card_seconds=60.0))
    ledger.append(path, record(
        exp_id="e1", supersedes_exp_id="e1", run_ids=["r1", "r2", "r3"], card_seconds=30.0,
    ))
    live, spent = ledger.fold(ledger.read_ledger(path))
    assert spent["card_seconds"] == 90.0 and live[0]["card_seconds"] == 90.0
    # Idempotent retry adding a lesson note must keep 90.0s, never drop back to 30.0s.
    retry_rec = record(
        exp_id="e1", run_ids=["r1", "r2", "r3"], card_seconds=90.0,
        lesson=dict(distill=dict(unit_id="u1", revision=1), note="verified"),
    )
    out = ledger.append(path, retry_rec, allow_idempotent_retry=True)
    assert out["recovered_append"] is True and out["revised_append"] is True
    live2, spent2 = ledger.fold(ledger.read_ledger(path))
    assert spent2["card_seconds"] == 90.0 and live2[0]["card_seconds"] == 90.0


def test_fingerprint_distinguishes_dirty_submodule_edits(tmp_path):
    sub_origin = tmp_path / "sub-origin"
    sub_origin.mkdir()
    git(sub_origin, "init", "-q")
    git(sub_origin, "config", "user.email", "t@example.invalid")
    git(sub_origin, "config", "user.name", "t")
    (sub_origin / "sub.py").write_text("v = 1\n", encoding="utf-8")
    git(sub_origin, "add", ".")
    git(sub_origin, "commit", "-qm", "sub init")

    repo = tmp_path / "main"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "main.py").write_text("m = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "main init")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "vendor/sub")
    git(repo, "commit", "-qm", "add sub")

    clean_fp = ledger.fingerprint(repo)
    assert clean_fp["dirty"] is False and clean_fp["submodules"] == 1
    (repo / "vendor" / "sub" / "sub.py").write_text("v = 2\n", encoding="utf-8")
    dirty_a = ledger.fingerprint(repo)
    assert dirty_a["dirty"] is True and dirty_a["source_digest"] != clean_fp["source_digest"]
    (repo / "vendor" / "sub" / "sub.py").write_text("v = 3\n", encoding="utf-8")
    dirty_b = ledger.fingerprint(repo)
    assert dirty_b["source_digest"] != dirty_a["source_digest"]


def test_gate_blocks_completed_noise_duplicate_but_allows_ablation_pending(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(
        exp_id="e-noise", source_digest="sha256:noise", verdict="inconclusive",
        outcome="speedup_within_noise", run_status="completed", speedup_ratio=1.002,
    ))
    dup = ledger.gate(path, task_id="t1", source_digest="sha256:noise", scope=dict(soc="910B"))
    assert dup["decision"] == "duplicate" and dup["prior"]["outcome"] == "speedup_within_noise"

    ledger.append(path, record(
        exp_id="e-abl", source_digest="sha256:abl", verdict="inconclusive",
        outcome="ablation_pending", run_status="completed", speedup_ratio=1.15,
    ))
    assert ledger.gate(path, task_id="t1", source_digest="sha256:abl", scope=dict(soc="910B"))["decision"] == "submit"


def test_judge_enforces_ablation_hygiene_guard_workload_and_hidden_input_gates():
    assert ledger.judge(evidence(ablation_ok=False))["verdict"] == "refuted"
    assert ledger.judge(evidence(ablation_ok=False))["outcome"] == "ablation_not_retained"
    assert ledger.judge(evidence(workspace_delta_bytes=9000))["verdict"] == "refuted"
    assert ledger.judge(evidence(workspace_delta_bytes=9000))["outcome"] == "workspace_delta_exceeded"
    assert ledger.judge(evidence(guard_workloads=[{"regression": 0.08}]))["verdict"] == "refuted"
    assert ledger.judge(evidence(guard_workloads=[{"regression": 0.08}]))["outcome"] == "guard_workload_regressed"
    assert ledger.judge(evidence(hidden_input_summary={"opt_regression": True}))["verdict"] == "refuted"
    assert ledger.judge(evidence(hidden_input_summary={"opt_regression": True}))["outcome"] == "oracle_failed"


def test_fingerprint_ignores_task_artifacts_and_enforces_stream_deadline(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "kernel.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")

    clean_fp = ledger.fingerprint(repo)
    (repo / "rsi-loop.json").write_text('{"schema": "mindie-rsi-loop/1"}\n', encoding="utf-8")
    (repo / "experiment-ledger.jsonl").write_text('{"schema": "mindie-experiment-ledger/1"}\n', encoding="utf-8")
    (repo / "experiment-ledger.jsonl.tmp").write_text("{}\n", encoding="utf-8")
    (repo / "PROGRESS.md").write_text("# RSI Progress\n", encoding="utf-8")
    (repo / "custom-state.json").write_text(
        json.dumps({"current": {"bottleneck": "x" * 600}, "schema": "mindie-rsi-loop/1"}, sort_keys=True),
        encoding="utf-8",
    )
    (repo / "custom-state.json.lock").write_text("", encoding="utf-8")
    assert ledger.fingerprint(repo) == clean_fp

    git(repo, "add", "custom-state.json")
    git(repo, "commit", "-qm", "track custom state")
    tracked_fp = ledger.fingerprint(repo)["source_digest"]
    (repo / "custom-state.json").write_text('{"schema": "mindie-rsi-loop/1", "round": 2}\n', encoding="utf-8")
    assert ledger.fingerprint(repo)["source_digest"] == tracked_fp

    with (repo / "kernel.py").open("rb") as handle, pytest.raises(subprocess.TimeoutExpired):
        ledger._stream_digest(handle, deadline=0.0)


