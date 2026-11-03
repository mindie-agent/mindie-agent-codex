"""Code-enforced RSI experiment ledger: source fingerprint, judgment, dedup and budget.

[Role]
Turns the `experiment-ledger.jsonl` rules that previously lived only in Skill
prose into a deterministic shell helper (`fingerprint`, `judge`, `append`,
`gate`) that the Agent runs with the configured runtime Python.

[Invariants]
- A judgment is `inconclusive` whenever evidence is incomplete (missing or
  failed trials, compile/infra failure, unverified correctness, missing or
  unchanged source fingerprint); incomplete evidence outranks a failed gate,
  which outranks a pass. Complete evidence is judged by the knowledge engine's
  `evaluate_exploration_candidate`; this adapter does not reimplement it.
- `append` rejects records that contradict their own run status (an infra or
  compile failure recorded as `refuted`) or lack a source fingerprint.
- `gate` blocks a submission whose source fingerprint and scope already have a
  supported/refuted record, or whose task exceeded the caller's run or
  card-second budget. An `inconclusive` record never blocks a retry.

[Timeouts & Locks]
- `fingerprint` runs bounded `git` subprocesses. Ledger appends hold an
  exclusive sidecar `<ledger>.lock` via `update_lock.file_lock`.

[Side-Effect Policy]
- Only `append` writes, and only one LF-terminated JSON line to the named ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA = "mindie-experiment-record/1"
JUDGMENTS = frozenset({"supported", "refuted", "inconclusive"})
# Execution outcomes describe missing evidence, never a refuted hypothesis
# (references/domain-skills.md: compile errors, SSH drops, invalid windows).
INCONCLUSIVE_STATUSES = frozenset({
    "compile_failed", "infra_failed", "measurement_invalid", "timeout",
    "interrupted", "unknown",
})
SETTLED = frozenset({"supported", "refuted"})
# source: per-call git bound used by InferRSI infra/targets/git.py
GIT_TIMEOUT = 15.0
# source: stdin bound used by knowledge_cli.py
ARGUMENT_LIMIT = 1024 * 1024
# source: knowledge engine evaluate_exploration_candidate default target
DEFAULT_TARGET = 1.05
# source: exit code for a blocked gate decision; 1 stays reserved for errors
BLOCKED = 3


# --------------------------------------------------------------------- fingerprint
def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=GIT_TIMEOUT, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()
                         or f"git {args[0]} failed")
    return result.stdout


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):  # streamed (Principle 18)
            digest.update(block)
    return digest.hexdigest()


def fingerprint(repo) -> dict:
    """Identify the exact source under test: HEAD, tracked diff, untracked files, submodules.

    Adapted from InferRSI `infra/targets/git.py:repository_identity` (Apache-2.0).
    """
    repo = Path(repo).resolve()
    root = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
    if root != repo:
        raise ValueError("repo must be the root of its Git checkout")
    head = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    diff = _git(repo, "diff", "--binary", "HEAD", "--", ".")
    untracked = []
    for raw in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if raw:
            name = os.fsdecode(raw)
            path = repo / name
            untracked.append([name, _file_digest(path) if path.is_file() else "non-file"])
    submodules = []
    for raw in _git(repo, "submodule", "status", "--recursive").splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            commit, _, rest = line.lstrip("+-U ").partition(" ")
            path = rest.split(" (", 1)[0]
            dirty = bool(_git(repo / path, "status", "--porcelain")) if (repo / path).is_dir() else None
            submodules.append([path, commit, dirty])
    material = dict(head=head, diff_sha256=hashlib.sha256(diff).hexdigest(),
                    untracked=sorted(untracked), submodules=sorted(submodules))
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()
    return dict(source_digest="sha256:" + digest, head=head,
                dirty=bool(diff or untracked or any(s[2] for s in submodules)),
                untracked_files=len(untracked), submodules=len(submodules))


# ------------------------------------------------------------------------- judge
def _metric(trial, label, index, incomplete):
    if not isinstance(trial, dict):
        incomplete.append(f"{label} trial {index}: not an object")
        return None
    if trial.get("status") != "completed":
        incomplete.append(f"{label} trial {index}: status {trial.get('status')!r}")
        return None
    value = trial.get("metric")
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        incomplete.append(f"{label} trial {index}: metric missing or not positive")
        return None
    return float(value)


def judge(evidence) -> dict:
    """Three-state judgment: inconclusive > refuted > supported.

    The evidence-completeness order follows InferRSI `harness/evaluation.py:evaluate`
    (Apache-2.0); complete evidence goes to the knowledge engine's gates.
    """
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    incomplete, failed = [], []
    repetitions = evidence.get("repetitions")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    direction = evidence.get("direction")
    if direction not in ("maximize", "minimize"):
        raise ValueError("direction must be maximize or minimize")
    target = evidence.get("target_speedup", DEFAULT_TARGET)
    if isinstance(target, bool) or not isinstance(target, (int, float)) \
            or not math.isfinite(target) or target <= 0:
        raise ValueError("target_speedup must be a positive finite number")
    pairs = {}
    for label in ("baseline", "candidate"):
        trials = evidence.get(label)
        if not isinstance(trials, list):
            raise ValueError(f"{label} must be an array of trials")
        if len(trials) != repetitions:
            incomplete.append(f"{label}: expected {repetitions} trials, recorded {len(trials)}")
        pairs[label] = [_metric(t, label, i, incomplete) for i, t in enumerate(trials, 1)]
    if evidence.get("compile_ok") is False:
        incomplete.append("candidate did not build; nothing was measured")
    correctness = evidence.get("correctness")
    if correctness == "failed":
        failed.append("correctness check failed")
    elif correctness != "passed":
        incomplete.append(f"correctness was not verified ({correctness!r})")
    base_src, cand_src = evidence.get("baseline_source_digest"), evidence.get("source_digest")
    if not cand_src or not base_src:
        incomplete.append("source fingerprint missing; the measured code is not identified")
    elif cand_src == base_src:
        incomplete.append("candidate source equals baseline source; timing noise cannot "
                          "establish an improvement")
    speedup = None
    complete = [(b, c) for b, c in zip(pairs["baseline"], pairs["candidate"])
                if b is not None and c is not None]
    if complete:
        # Median of paired ratios, as MindIE Bench scores paired rounds.
        ratios = [c / b if direction == "maximize" else b / c for b, c in complete]
        speedup = statistics.median(ratios)
    engine = None
    if not incomplete and not failed:
        from mindie_knowledge.graded.explore import evaluate_exploration_candidate
        engine = evaluate_exploration_candidate(
            dict(compile_ok=True, correctness_ok=True, speedup=speedup,
                 conditions=dict(evidence.get("scope_conditions") or {}),
                 hypothesis=str(evidence.get("hypothesis") or "")),
            target_speedup=float(target))
        if engine["verdict"] not in ("distill_candidate", "retain_verified_evidence"):
            failed.append(f"{engine['reason']}: median paired speedup {speedup:.4g} "
                          f"below target {float(target):.4g}")
    judgment = "inconclusive" if incomplete else "refuted" if failed else "supported"
    return dict(judgment=judgment, reasons=incomplete + failed, speedup=speedup,
                target_speedup=float(target), paired_rounds=len(complete),
                engine_verdict=engine and engine["verdict"])


# ------------------------------------------------------------------------ ledger
def read_ledger(path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    records = []
    with path.open("r", encoding="utf-8", newline="\n") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc
    return records


def validate(record) -> dict:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    for key in ("task_id", "exp_id", "hypothesis", "source_digest"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    run_ids = record.get("run_ids")
    if not isinstance(run_ids, list) or any(not isinstance(r, str) or not r for r in run_ids):
        raise ValueError("run_ids must be an array of run_id strings")
    judgment = record.get("judgment")
    if judgment not in JUDGMENTS:
        raise ValueError("judgment must be supported, refuted or inconclusive")
    status = record.get("run_status")
    if status in INCONCLUSIVE_STATUSES and judgment != "inconclusive":
        raise ValueError(f"run_status {status!r} is missing evidence; judgment must be "
                         "inconclusive, not " + judgment)
    seconds = record.get("card_seconds")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                                or not math.isfinite(seconds) or seconds < 0):
        raise ValueError("card_seconds must be a nonnegative number")
    scope = record.get("scope_conditions")
    if scope is not None and not isinstance(scope, dict):
        raise ValueError("scope_conditions must be an object")
    return dict(record, schema=SCHEMA)


def append(path, record) -> dict:
    from update_lock import file_lock
    entry = validate(record)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path) + ".lock", exclusive=True, blocking=True):
        if entry.get("supersedes_exp_id") is None and any(
                r.get("task_id") == entry["task_id"] and r.get("exp_id") == entry["exp_id"]
                for r in read_ledger(path)):
            raise ValueError(f"exp_id {entry['exp_id']!r} already recorded; set "
                             "supersedes_exp_id to revise it")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return dict(status="recorded", exp_id=entry["exp_id"], judgment=entry["judgment"])


def gate(path, *, task_id, source_digest, scope=None, max_runs=None, max_card_seconds=None):
    """Decide before a candidate goes to the device: submit, duplicate or budget_exhausted.

    Mechanism follows vllm-evolve `bench/eval_pool.py` (HashCache + EvalBudget).
    """
    records = [r for r in read_ledger(path) if r.get("task_id") == task_id]
    superseded = {r.get("supersedes_exp_id") for r in records if r.get("supersedes_exp_id")}
    live = [r for r in records if r.get("exp_id") not in superseded]
    runs = sum(len(r.get("run_ids") or ()) for r in records)
    seconds = sum(float(r.get("card_seconds") or 0) for r in records)
    spent = dict(runs=runs, card_seconds=seconds, max_runs=max_runs,
                 max_card_seconds=max_card_seconds)
    for record in reversed(live):
        if record.get("source_digest") == source_digest and record.get("judgment") in SETTLED \
                and (scope is None or record.get("scope_conditions") == scope):
            return dict(decision="duplicate", spent=spent,
                        prior=dict(exp_id=record.get("exp_id"), judgment=record["judgment"],
                                   run_ids=record.get("run_ids")),
                        reason="this exact source was already judged under the same scope")
    if max_runs is not None and runs >= max_runs:
        return dict(decision="budget_exhausted", spent=spent,
                    reason=f"{runs} device runs recorded; budget is {max_runs}")
    if max_card_seconds is not None and seconds >= max_card_seconds:
        return dict(decision="budget_exhausted", spent=spent,
                    reason=f"{seconds:g} card-seconds recorded; budget is {max_card_seconds:g}")
    return dict(decision="submit", spent=spent)


# --------------------------------------------------------------------------- CLI
def _stdin_json():
    raw = sys.stdin.buffer.read(ARGUMENT_LIMIT + 1)
    if len(raw) > ARGUMENT_LIMIT:
        raise ValueError("input exceeds 1 MiB")
    return json.loads(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="experiment_ledger.py")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fingerprint").add_argument("repo")
    commands.add_parser("judge")
    commands.add_parser("append").add_argument("--ledger", required=True)
    check = commands.add_parser("gate")
    check.add_argument("--ledger", required=True)
    check.add_argument("--task-id", required=True)
    check.add_argument("--source-digest", required=True)
    check.add_argument("--scope", type=json.loads)
    check.add_argument("--max-runs", type=int)
    check.add_argument("--max-card-seconds", type=float)
    args = parser.parse_args(argv)
    try:
        if args.command == "fingerprint":
            result = fingerprint(args.repo)
        elif args.command == "judge":
            result = judge(_stdin_json())
        elif args.command == "append":
            result = append(args.ledger, _stdin_json())
        else:
            result = gate(args.ledger, task_id=args.task_id, source_digest=args.source_digest,
                          scope=args.scope, max_runs=args.max_runs,
                          max_card_seconds=args.max_card_seconds)
        code = BLOCKED if result.get("decision") in ("duplicate", "budget_exhausted") else 0
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        result, code = dict(status="error", error=f"{type(exc).__name__}: {exc}"), 1
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
