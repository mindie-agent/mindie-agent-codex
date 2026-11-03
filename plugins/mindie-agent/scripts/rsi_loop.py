"""Resumable RSI optimization loop: one checked step at a time, judged and recorded by MindIE Bench.

[Role]
Drives `profile -> consult -> propose -> measure -> learn` as a persisted step
machine the Agent advances with `next` / `complete`. MCP steps stay with the
Agent; `propose` fingerprints and gates through `experiment_ledger`, `measure`
judges through `experiment_ledger.judge` (MindIE Bench `evaluate_rsi_candidate_gates`),
`learn` appends through MindIE Bench (ledger line + `PROGRESS.md`) and asks
`assess_stagnation` whether the next round must explore.

[Invariants]
- `complete` accepts only the current step; anything else is rejected without
  touching state, so a resumed session continues from `next`.
- A candidate reaches the device only after `gate` returns `submit`; a duplicate
  stays at `propose`, an exhausted budget finishes the loop.
- `measure` re-fingerprints the candidate and refuses evidence whose source
  changed after the gate. An execution-failure `run_status` is always `inconclusive`.
- `supported` / `subtarget_valid` moves the baseline; otherwise the next round
  reuses the profile and starts at `consult`. Lessons are optional; a kept gain
  without a `distill` receipt stays in `pending_distill`.

[Timeouts & Locks]
- State changes hold `<state>.lock` (`experiment_ledger.exclusive_lock`, bounded
  wait) and replace the state file atomically. Fingerprints use the bounded
  `git` calls of `experiment_ledger`; no device, network or model call here.

[Side-Effect Policy]
- Writes only the state file, its lock, and the ledger + `PROGRESS.md` through
  `experiment_ledger.append`.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import experiment_ledger as ledger  # noqa: E402

SCHEMA = "mindie-rsi-loop/2"
STEPS = ("profile", "consult", "propose", "measure", "learn")
KEPT = frozenset({"supported", "subtarget_valid"})  # MindIE Bench distill_ready verdicts
BLOCKED = ledger.BLOCKED  # source: exit code for a blocked gate decision in experiment_ledger.py
STALL_WINDOW = 3  # source: knowledge engine assess_stagnation default window (SKILL.md)
MEASURE_OPTIONAL = ("ablation_retained", "ablation_receipt", "hidden_input_summary",
                    "noise_margin")

GUIDE = {
    "profile": ("Hold an exclusive coordinator lease and run mindie_profiler with an explicit "
                "--time-window/--step-window/--full-range on the current baseline. Complete with "
                '{"bottleneck_summary": {...}, "profile_ref": "<receipt or path>"}.'),
    "consult": ("When /mindie-agent knowledge is enabled and an information gap remains, call "
                "knowledge_query(query=<bottleneck>, mode=<mode>) and read the cards you will "
                "rely on with knowledge_explain. In explore mode pass native_subcontext=True, "
                "ledger_entries and bottleneck_summary, and seed a fresh subagent only with "
                "exploration_context.subagent_fact_pack.prompt_brief. Complete with "
                '{"mode": <mode>, "unit_refs": [{"unit_id", "revision"}]}, or with '
                '{"mode": <mode>, "unit_refs": [], "skipped_reason": "<why no query>"}.'),
    "propose": ("Change the candidate checkout for exactly one hypothesis. Prefer a separate git "
                "worktree for the candidate; if it shares the baseline checkout, first restore "
                "that checkout to the baseline source. Complete with "
                '{"exp_id", "hypothesis", "candidate_repo", "scope_conditions": {...}, '
                '"factors": [...]}; the loop fingerprints it and runs the ledger gate. '
                "Exit 3 means do not submit."),
    "measure": ("Submit baseline and candidate through coordinator/remote-dev on the same device, "
                "alternating AB/BA, and register every run_id in PROGRESS.md in_flight_runs. If "
                "baseline and candidate share one checkout, leave it at the candidate code before "
                "completing: the loop re-fingerprints it. Complete with "
                '{"run_ids", "baseline": [{"status", "metric"}], "candidate": [...], '
                '"correctness": "passed|failed", "compile_ok", "run_status", "card_seconds"} '
                "plus any of ablation_retained, ablation_receipt (required verdict input when "
                "several factors changed), hidden_input_summary, noise_margin."),
    "learn": ("Optionally keep the lesson: pass the knowledge_distill receipt as "
              '{"distill": {...}} and/or a short {"note": "..."}; new_observation / '
              "divergent_layer_identified (booleans) count as progress for the stagnation "
              "check. A kept gain without distill stays in pending_distill."),
}


# ------------------------------------------------------------------------- state
def _load(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"{path} does not exist; run init first")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} state file")
    return state


def _save(path: Path, state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _positive(value, label, *, integer=False, optional=False):
    if value is None and optional:
        return None
    if integer:
        if type(value) is not int or value < 1:
            raise ValueError(f"{label} must be a positive integer")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive number")
    return float(value)


def _text(body, key) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be nonempty text")
    return value.strip()


def init(path, *, task_id, ledger_path, baseline_repo, direction, repetitions,
         target_speedup=ledger.DEFAULT_TARGET, max_rounds=None, max_runs=None,
         max_card_seconds=None, progress=None, goal=None) -> dict:
    path = Path(path).resolve()
    if direction not in ("maximize", "minimize"):
        raise ValueError("direction must be maximize or minimize")
    ledger_path = Path(ledger_path).resolve()
    baseline = ledger.fingerprint(baseline_repo)
    state = dict(
        schema=SCHEMA, task_id=task_id, ledger=str(ledger_path),
        progress=str(Path(progress).resolve() if progress else ledger_path.parent / "PROGRESS.md"),
        goal=goal or f"Optimize {task_id}",
        direction=direction, repetitions=_positive(repetitions, "repetitions", integer=True),
        target_speedup=_positive(target_speedup, "target_speedup"),
        max_rounds=_positive(max_rounds, "max_rounds", integer=True, optional=True),
        max_runs=_positive(max_runs, "max_runs", integer=True, optional=True),
        max_card_seconds=_positive(max_card_seconds, "max_card_seconds", optional=True),
        baseline=dict(repo=str(Path(baseline_repo).resolve()),
                      source_digest=baseline["source_digest"], exp_id=None),
        round=1, step="profile", mode="reuse", status="running", finish_reason=None,
        current=dict(skipped=[]), rounds=[], pending_distill=[])
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger.exclusive_lock(path):
        if path.exists():
            raise ValueError(f"{path} already exists; use next to resume it")
        _save(path, state)
    return next_step(path)


def next_step(path) -> dict:
    state = _load(Path(path).resolve())
    if state["status"] != "running":
        return dict(status=state["status"], finish_reason=state["finish_reason"],
                    rounds=len(state["rounds"]), progress=state["progress"],
                    pending_distill=state.get("pending_distill") or [])
    guide = GUIDE[state["step"]].replace("<mode>", repr(state["mode"]))
    result = dict(status="running", round=state["round"], step=state["step"],
                  mode=state["mode"], guide=guide, progress=state["progress"],
                  baseline_source_digest=state["baseline"]["source_digest"])
    if state["step"] == "learn":
        result["verdict"] = state["current"]["entry"]["verdict"]
    if state["current"].get("skipped"):
        result["skipped_this_round"] = state["current"]["skipped"]
    if state["current"].get("profile_reused_from") is not None:
        result["profile_reused_from_round"] = state["current"]["profile_reused_from"]
    if state.get("restore_hint"):
        result["restore_hint"] = state["restore_hint"]
    if state.get("pending_distill"):
        result["pending_distill"] = state["pending_distill"]
    return result


# ------------------------------------------------------------------------- steps
def _profile(state, body):
    if not isinstance(body.get("bottleneck_summary"), dict) or not body["bottleneck_summary"]:
        raise ValueError("bottleneck_summary must be a nonempty object from the profiler")
    state["current"].update(bottleneck_summary=body["bottleneck_summary"],
                            profile_ref=_text(body, "profile_ref"))
    return "consult", {}


def _consult(state, body):
    if body.get("mode") != state["mode"]:
        raise ValueError(f"this round must query knowledge in {state['mode']!r} mode")
    refs = body.get("unit_refs")
    if not isinstance(refs, list) or any(
            not isinstance(r, dict) or not r.get("unit_id") or type(r.get("revision")) is not int
            for r in refs):
        raise ValueError("unit_refs must be an array of {unit_id, revision} objects")
    skipped = body.get("skipped_reason")
    if skipped is not None and (not isinstance(skipped, str) or not skipped.strip()):
        raise ValueError("skipped_reason must be nonempty text when given")
    state["current"].update(unit_refs=refs, knowledge_mode=state["mode"],
                            knowledge_skipped=skipped and skipped.strip())
    return "propose", {}


def _propose(state, body):
    current = state["current"]
    hypothesis, exp_id = _text(body, "hypothesis"), _text(body, "exp_id")
    scope = body.get("scope_conditions")
    if scope is not None and not isinstance(scope, dict):
        raise ValueError("scope_conditions must be an object")
    factors = body.get("factors")
    if factors is not None and (not isinstance(factors, list)
                                or any(not isinstance(f, str) or not f for f in factors)):
        raise ValueError("factors must be an array of factor names")
    repo = str(Path(_text(body, "candidate_repo")).resolve())
    digest = ledger.fingerprint(repo)["source_digest"]
    if digest == state["baseline"]["source_digest"]:
        raise ValueError("candidate source equals the baseline; change the code before proposing")
    state.pop("restore_hint", None)
    decision = ledger.gate(state["ledger"], task_id=state["task_id"], source_digest=digest,
                           scope=scope, max_runs=state["max_runs"],
                           max_card_seconds=state["max_card_seconds"])
    attempt = dict(exp_id=exp_id, hypothesis=hypothesis, source_digest=digest,
                   decision=decision["decision"], reason=decision.get("reason"),
                   prior=decision.get("prior"))
    if decision["decision"] == "duplicate":
        current["skipped"].append(attempt)
        return "propose", dict(gate=decision, blocked=True)
    if decision["decision"] == "budget_exhausted":
        current["skipped"].append(attempt)
        _finish(state, "budget_exhausted: " + decision["reason"])
        return None, dict(gate=decision, blocked=True)
    current["candidate"] = dict(exp_id=exp_id, hypothesis=hypothesis, repo=repo,
                                source_digest=digest, scope_conditions=scope,
                                factors=factors, spent=decision["spent"])
    return "measure", dict(gate=decision)


def _measure(state, body):
    candidate = state["current"]["candidate"]
    now = ledger.fingerprint(candidate["repo"])["source_digest"]
    if now != candidate["source_digest"]:
        state["current"].pop("candidate")
        return "propose", dict(error="candidate source changed after the gate; the measured "
                                     "code is not the gated code. Propose it again.")
    run_ids = body.get("run_ids")
    if not isinstance(run_ids, list) or not run_ids \
            or any(not isinstance(r, str) or not r for r in run_ids):
        raise ValueError("run_ids must be a nonempty array of run_id strings")
    seconds = body.get("card_seconds")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                                or not math.isfinite(seconds) or seconds < 0):
        raise ValueError("card_seconds must be a nonnegative number")
    status = body.get("run_status") or "completed"
    evidence = dict(repetitions=state["repetitions"], direction=state["direction"],
                    target_speedup=state["target_speedup"],
                    baseline=body.get("baseline"), candidate=body.get("candidate"),
                    correctness=body.get("correctness"), compile_ok=body.get("compile_ok"),
                    run_status=status,
                    baseline_source_digest=state["baseline"]["source_digest"],
                    source_digest=candidate["source_digest"],
                    scope_conditions=candidate["scope_conditions"],
                    run_ids=run_ids, exp_id=candidate["exp_id"],
                    hypothesis=candidate["hypothesis"], factors=candidate["factors"])
    for key in MEASURE_OPTIONAL:
        if key in body:
            evidence[key] = body[key]
    entry = ledger.judge(evidence)
    state["current"].update(
        run_ids=run_ids, run_status=status, card_seconds=seconds,
        receipt_refs=body.get("receipt_refs"),
        trials=dict(baseline=evidence["baseline"], candidate=evidence["candidate"],
                    correctness=evidence["correctness"], compile_ok=evidence["compile_ok"]),
        entry=entry)
    return "learn", dict(entry=entry)


def _stagnation(state) -> dict:
    from mindie_knowledge.graded.explore import assess_stagnation
    live, _ = ledger.fold(r for r in ledger.read_ledger(state["ledger"])
                          if r.get("task_id") == state["task_id"])
    entries = []
    for r in live:
        # One entry per experiment: the engine dedups by run_id, and a reused
        # baseline run in run_ids must not fold several rounds into one.
        entry = dict(run_id=r["exp_id"], verdict=r["verdict"],
                     outcome=r.get("outcome") or r.get("run_status"),
                     hypothesis=r.get("mutation_summary"),
                     hypothesis_verified=r["verdict"] in KEPT)
        for key in ("new_observation", "divergent_layer_identified"):
            if key in r:
                entry[key] = r[key]
        entries.append(entry)
    return assess_stagnation(entries, consecutive_limit=STALL_WINDOW)


def _learn(state, body):
    current = state["current"]
    candidate, entry = current["candidate"], current["entry"]
    verdict = entry["verdict"]
    distill = body.get("distill")
    if distill is not None and (not isinstance(distill, dict) or not distill):
        raise ValueError("distill must be the nonempty knowledge_distill receipt when given")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise ValueError("note must be nonempty text when given")
    note = note and note.strip()
    record = dict(entry, task_id=state["task_id"], exp_id=candidate["exp_id"],
                  run_ids=current["run_ids"], source_digest=candidate["source_digest"],
                  baseline_source_digest=state["baseline"]["source_digest"],
                  run_status=current["run_status"], card_seconds=current.get("card_seconds"),
                  unit_refs=current.get("unit_refs") or [],
                  receipt_refs=dict(current.get("receipt_refs") or {},
                                    profile=current.get("profile_ref")),
                  factors=candidate.get("factors"), trials=current["trials"],
                  knowledge_skipped=current.get("knowledge_skipped"),
                  lesson=dict(distill=distill, note=note))
    for key in ("new_observation", "divergent_layer_identified"):
        if type(body.get(key)) is bool:
            record[key] = body[key]
    ledger.append(state["ledger"], record, progress=state["progress"], goal=state["goal"])
    stagnation = _stagnation(state)
    current["stagnation"] = dict(status=stagnation.get("status"), reason=stagnation.get("reason"))
    previous = dict(state["baseline"])
    if verdict in KEPT and distill is None:
        state.setdefault("pending_distill", []).append(
            dict(round=state["round"], exp_id=candidate["exp_id"],
                 reason=note or "not distilled yet"))
    if verdict in KEPT:
        state["baseline"] = dict(repo=candidate["repo"], source_digest=candidate["source_digest"],
                                 exp_id=candidate["exp_id"])
        state.pop("restore_hint", None)
    elif candidate["repo"] == previous["repo"]:
        state["restore_hint"] = (f"the candidate shared the baseline checkout {previous['repo']}; "
                                 f"restore it to baseline source {previous['source_digest']} "
                                 "before the next change")
    state["rounds"].append(dict(round=state["round"], exp_id=candidate["exp_id"],
                                verdict=verdict, outcome=entry.get("outcome"),
                                speedup_ratio=entry.get("speedup_ratio"),
                                stagnation=stagnation.get("status")))
    state["mode"] = "explore" if stagnation.get("stalled") else "reuse"
    extra = dict(progress=state["progress"], stagnation=current["stagnation"])
    if state["max_rounds"] is not None and state["round"] >= state["max_rounds"]:
        _finish(state, f"max_rounds {state['max_rounds']} reached")
        return None, extra
    state["round"] += 1
    if verdict not in KEPT and current.get("profile_ref"):
        # The baseline did not change, so its profile still holds (GEAK reprofile rule).
        state["current"] = dict(skipped=[], bottleneck_summary=current["bottleneck_summary"],
                                profile_ref=current["profile_ref"],
                                profile_reused_from=current.get("profile_reused_from")
                                or state["round"] - 1)
        upcoming = "consult"
    else:
        state["current"] = dict(skipped=[])
        upcoming = "profile"
    extra["next_mode"] = state["mode"]
    return upcoming, extra


HANDLERS = dict(profile=_profile, consult=_consult, propose=_propose,
                measure=_measure, learn=_learn)


def _finish(state, reason):
    state.update(status="finished", finish_reason=reason, step=None)


def complete(path, step, body) -> tuple[dict, int]:
    path = Path(path).resolve()
    if not isinstance(body, dict):
        raise ValueError("step evidence must be a JSON object")
    with ledger.exclusive_lock(path):
        state = _load(path)
        if state["status"] != "running":
            raise ValueError(f"loop is {state['status']}: {state['finish_reason']}")
        if step != state["step"]:
            raise ValueError(f"current step is {state['step']!r} (round {state['round']}); "
                             f"{step!r} is out of order. Run next to resume.")
        upcoming, extra = HANDLERS[step](state, body)
        if upcoming is not None:
            state["step"] = upcoming
        _save(path, state)
    code = BLOCKED if extra.get("blocked") else 1 if extra.get("error") else 0
    return dict(next_step(path), completed=step, **extra), code


def finish(path, reason) -> dict:
    path = Path(path).resolve()
    with ledger.exclusive_lock(path):
        state = _load(path)
        if state["status"] == "running":
            _finish(state, reason)
            _save(path, state)
    return next_step(path)


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rsi_loop.py")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("init")
    start.add_argument("--state", required=True)
    start.add_argument("--task-id", required=True)
    start.add_argument("--ledger", required=True)
    start.add_argument("--baseline-repo", required=True)
    start.add_argument("--direction", required=True, choices=("maximize", "minimize"))
    start.add_argument("--repetitions", required=True, type=int)
    start.add_argument("--target-speedup", type=float, default=ledger.DEFAULT_TARGET)
    start.add_argument("--max-rounds", type=int)
    start.add_argument("--max-runs", type=int)
    start.add_argument("--max-card-seconds", type=float)
    start.add_argument("--progress")
    start.add_argument("--goal")
    commands.add_parser("next").add_argument("--state", required=True)
    done = commands.add_parser("complete")
    done.add_argument("step", choices=STEPS)
    done.add_argument("--state", required=True)
    stop = commands.add_parser("finish")
    stop.add_argument("--state", required=True)
    stop.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    code = 0
    try:
        if args.command == "init":
            result = init(args.state, task_id=args.task_id, ledger_path=args.ledger,
                          baseline_repo=args.baseline_repo, direction=args.direction,
                          repetitions=args.repetitions, target_speedup=args.target_speedup,
                          max_rounds=args.max_rounds, max_runs=args.max_runs,
                          max_card_seconds=args.max_card_seconds, progress=args.progress,
                          goal=args.goal)
        elif args.command == "next":
            result = next_step(args.state)
        elif args.command == "complete":
            result, code = complete(args.state, args.step, ledger._stdin_json())
        else:
            result = finish(args.state, args.reason)
    except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired,
            json.JSONDecodeError) as exc:
        result, code = dict(status="error", error=f"{type(exc).__name__}: {exc}"), 1
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
