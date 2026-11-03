"""Pre-device checks around the MindIE Bench RSI ledger: what was measured, was it already judged.

[Role]
Adds what `mindie_bench.runner.rsi_explorer` does not do: identify the exact
source under test (`fingerprint`), check raw paired trials before trusting a
speedup (`judge`), and block a device submission that repeats a judged source or
exceeds the run / card-second budget (`gate`). Judgment, the ledger line format
and `PROGRESS.md` are MindIE Bench's (`evaluate_rsi_candidate_gates`,
`append_ledger_and_render_progress`); this adapter does not reimplement them.

[Invariants]
- Raw trials decide the evidence flags handed to the bench gate: missing or
  failed trials, an unbuilt candidate, an execution-failure `run_status`,
  unverified correctness or a missing or unchanged source fingerprint make
  `measurement_passed` false, so the bench verdict is `inconclusive`, never
  `refuted` (missing evidence also outranks a failed correctness check).
- `append` refuses an execution failure that is not `inconclusive`, a record
  without a source fingerprint, and a reused `exp_id` without `supersedes_exp_id`.
- `gate` folds `supersedes_exp_id` revisions, counts distinct `run_ids` once, and
  blocks only a live `supported` / `subtarget_valid` / `refuted` record with the
  same source and conditions (missing conditions equal `{}`).

[Timeouts & Locks]
- `fingerprint` bounds every `git` call and the whole scan, streams the diff
  through a temporary file and identifies untracked files above 512 MiB by size.
- `append` holds `<ledger>.lock` through `mindie_knowledge.loop.locks.StartLock`
  for at most LOCK_WAIT_SECONDS, then raises `TimeoutError`.

[Side-Effect Policy]
- Only `append` writes: one ledger line and `PROGRESS.md` through MindIE Bench, as UTF-8 LF.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

SETTLED = frozenset({"supported", "subtarget_valid", "refuted"})
# Execution outcomes describe missing evidence, never a refuted hypothesis
# (references/domain-skills.md: compile errors, SSH drops, invalid windows).
INCONCLUSIVE_STATUSES = frozenset({
    "compile_failed", "infra_failed", "measurement_invalid", "timeout",
    "interrupted", "unknown",
})
GIT_TIMEOUT = 15.0  # source: per-call git bound used by InferRSI infra/targets/git.py
FINGERPRINT_TIMEOUT = 4 * GIT_TIMEOUT  # source: four git calls of a submodule-free fingerprint
LARGE_FILE_BYTES = 512 * 1024 * 1024  # source: git core.bigFileThreshold default (512 MiB)
LOCK_WAIT_SECONDS = 5.0  # source: knowledge AGENTS.md lock rule (bounded 5.0 s waits)
DEFAULT_TARGET = 1.05  # source: MindIE Bench DEFAULT_MIN_SPEEDUP_RATIO
DEFAULT_NOISE_MARGIN = 0.01  # source: MindIE Bench evaluate_rsi_candidate_gates noise_margin default
ARGUMENT_LIMIT = 1024 * 1024  # source: stdin bound used by knowledge_cli.py
BLOCKED = 3  # source: existing gate exit code of this helper; 1 stays reserved for errors


# --------------------------------------------------------------------- fingerprint
def _git(repo: Path, *args: str, deadline: float, stdout=subprocess.PIPE) -> bytes:
    timeout = min(GIT_TIMEOUT, deadline - time.monotonic())
    if timeout <= 0:
        raise subprocess.TimeoutExpired(["git", *args], FINGERPRINT_TIMEOUT)
    result = subprocess.run(["git", "-C", str(repo), *args], stdout=stdout,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()
                         or f"git {args[0]} failed")
    return result.stdout


def _stream_digest(handle) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: handle.read(1 << 20), b""):  # streamed (Principle 18)
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def fingerprint(repo) -> dict:
    """Identify the exact source under test: HEAD, tracked diff, untracked files, submodules.

    Adapted from InferRSI `infra/targets/git.py:repository_identity` (Apache-2.0).
    """
    deadline = time.monotonic() + FINGERPRINT_TIMEOUT
    repo = Path(repo).resolve()
    top = _git(repo, "rev-parse", "--show-toplevel", deadline=deadline)
    if Path(os.fsdecode(top).strip()).resolve() != repo:
        raise ValueError("repo must be the root of its Git checkout")
    head = _git(repo, "rev-parse", "HEAD", deadline=deadline).decode("ascii").strip()
    with tempfile.TemporaryFile() as spool:
        _git(repo, "diff", "--binary", "HEAD", "--", ".", deadline=deadline, stdout=spool)
        spool.seek(0)
        diff_sha256, diff_bytes = _stream_digest(spool)
    untracked, large = [], []
    listed = _git(repo, "ls-files", "--others", "--exclude-standard", "-z", deadline=deadline)
    for raw in filter(None, listed.split(b"\0")):
        name = os.fsdecode(raw)
        path = repo / name
        if not path.is_file():
            untracked.append([name, "non-file"])
        elif path.stat().st_size > LARGE_FILE_BYTES:
            untracked.append([name, f"size:{path.stat().st_size}"])
            large.append(name)
        else:
            with path.open("rb") as handle:
                untracked.append([name, _stream_digest(handle)[0]])
        if time.monotonic() > deadline:
            raise subprocess.TimeoutExpired(["fingerprint", str(repo)], FINGERPRINT_TIMEOUT)
    submodules = []
    for raw in _git(repo, "submodule", "status", "--recursive", deadline=deadline).splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            commit, _, rest = line.lstrip("+-U ").partition(" ")
            path = rest.split(" (", 1)[0]
            dirty = bool(_git(repo / path, "status", "--porcelain", deadline=deadline)) \
                if (repo / path).is_dir() else None
            submodules.append([path, commit, dirty])
    material = dict(head=head, diff_sha256=diff_sha256,
                    untracked=sorted(untracked), submodules=sorted(submodules))
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()
    result = dict(source_digest="sha256:" + digest, head=head,
                  dirty=bool(diff_bytes or untracked or any(s[2] for s in submodules)),
                  untracked_files=len(untracked), submodules=len(submodules))
    if large:
        result["unhashed_large_files"] = sorted(large)
    return result


# ------------------------------------------------------------------------- judge
def _number(value, label, *, strict=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or (value <= 0 if strict else value < 0):
        raise ValueError(f"{label} must be a {'positive' if strict else 'nonnegative'} number")
    return float(value)


def _metric(trial, label, index, problems):
    if not isinstance(trial, dict) or trial.get("status") != "completed":
        status = trial.get("status") if isinstance(trial, dict) else "not an object"
        problems.append(f"{label} trial {index}: status {status!r}")
        return None
    value = trial.get("metric")
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        problems.append(f"{label} trial {index}: metric missing or not positive")
        return None
    return float(value)


def check_trials(evidence) -> dict:
    """Turn raw paired trials into the evidence flags and ratio the bench gate trusts.

    The completeness order follows InferRSI `harness/evaluation.py:evaluate` (Apache-2.0).
    """
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    repetitions = evidence.get("repetitions")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    direction = evidence.get("direction")
    if direction not in ("maximize", "minimize"):
        raise ValueError("direction must be maximize or minimize")
    problems, pairs = [], {}
    for label in ("baseline", "candidate"):
        trials = evidence.get(label)
        if not isinstance(trials, list):
            raise ValueError(f"{label} must be an array of trials")
        if len(trials) != repetitions:
            problems.append(f"{label}: expected {repetitions} trials, recorded {len(trials)}")
        pairs[label] = [_metric(t, label, i, problems) for i, t in enumerate(trials, 1)]
    base_src, cand_src = evidence.get("baseline_source_digest"), evidence.get("source_digest")
    if not cand_src or not base_src:
        problems.append("source fingerprint missing; the measured code is not identified")
    elif cand_src == base_src:
        problems.append("candidate source equals baseline source")
    correctness = evidence.get("correctness")
    if correctness not in ("passed", "failed"):
        problems.append(f"correctness was not verified ({correctness!r})")
    complete = [(b, c) for b, c in zip(pairs["baseline"], pairs["candidate"])
                if b is not None and c is not None]
    ratio = statistics.median(c / b if direction == "maximize" else b / c
                              for b, c in complete) if complete else None
    status = evidence.get("run_status")
    if status in INCONCLUSIVE_STATUSES and status != "compile_failed":
        problems.insert(0, f"run_status {status!r} means evidence is missing")
    compile_ok = evidence.get("compile_ok") is not False and status != "compile_failed"
    if not compile_ok:
        problems.insert(0, "candidate did not build; nothing was measured")
    # Missing evidence outranks a failed oracle: the bench gate checks the oracle
    # before the measurement, so a correctness failure counts only when the
    # measurement itself is complete.
    return dict(compile_ok=compile_ok, oracle_passed=correctness != "failed" or bool(problems),
                measurement_passed=compile_ok and not problems, speedup_ratio=ratio,
                paired_rounds=len(complete), problems=problems)


def _ablation_retained(evidence):
    # A single-factor change is its own leave-one-out; several factors need a receipt.
    if "ablation_retained" in evidence:
        return evidence["ablation_retained"]
    factors = evidence.get("factors")
    return True if not factors or len(factors) <= 1 else None


def judge(evidence) -> dict:
    """Check raw trials, then let MindIE Bench `evaluate_rsi_candidate_gates` decide."""
    from mindie_bench.runner.rsi_explorer import evaluate_rsi_candidate_gates
    checked = check_trials(evidence)
    run_ids = evidence.get("run_ids") or []
    entry = evaluate_rsi_candidate_gates(
        run_id=",".join(run_ids), candidate_id=str(evidence.get("exp_id") or ""),
        mutation_summary=str(evidence.get("hypothesis") or ""),
        conditions=dict(evidence.get("scope_conditions") or {}),
        compile_ok=checked["compile_ok"], oracle_passed=checked["oracle_passed"],
        measurement_passed=checked["measurement_passed"], speedup_ratio=checked["speedup_ratio"],
        min_speedup_ratio=_number(evidence.get("target_speedup", DEFAULT_TARGET),
                                  "target_speedup"),
        noise_margin=_number(evidence.get("noise_margin", DEFAULT_NOISE_MARGIN),
                             "noise_margin", strict=False),
        ablation_retained=_ablation_retained(evidence),
        failure_reason="; ".join(checked["problems"]),
        hidden_input_summary=evidence.get("hidden_input_summary"),
        ablation_receipt=evidence.get("ablation_receipt"))
    entry["paired_rounds"] = checked["paired_rounds"]
    if checked["problems"]:
        entry["evidence_problems"] = checked["problems"]
    return entry


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


@contextlib.contextmanager
def exclusive_lock(path):
    from mindie_knowledge.loop.locks import StartInProgress, StartLock
    lock = StartLock(str(path) + ".lock", wait=LOCK_WAIT_SECONDS)
    try:
        lock.acquire(write_record=False)
    except StartInProgress:
        raise TimeoutError(f"{path} is locked by another writer for more than "
                           f"{LOCK_WAIT_SECONDS:g}s") from None
    try:
        yield
    finally:
        lock.release()


def validate(record) -> dict:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    for key in ("task_id", "exp_id", "source_digest", "verdict"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    run_ids = record.get("run_ids")
    if not isinstance(run_ids, list) or any(not isinstance(r, str) or not r for r in run_ids):
        raise ValueError("run_ids must be an array of run_id strings")
    status = record.get("run_status")
    if status in INCONCLUSIVE_STATUSES and record["verdict"] != "inconclusive":
        raise ValueError(f"run_status {status!r} is missing evidence; verdict must be "
                         "inconclusive, not " + record["verdict"])
    if record.get("card_seconds") is not None:
        _number(record["card_seconds"], "card_seconds", strict=False)
    if record.get("conditions") is not None and not isinstance(record["conditions"], dict):
        raise ValueError("conditions must be an object")
    return record


def _append_json_line(path, entry) -> None:
    """Append one JSON object as a single LF-terminated line.

    MindIE Bench `rsi_explorer._append_json_line` uses `os.open` + `os.write`.
    On Windows that is CRT text mode unless `O_BINARY` is set, so a lone `\\n`
    becomes `\\r\\n`. `newline="\\n"` keeps the on-disk ledger portable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def append(path, record, *, progress=None, goal="") -> dict:
    """Validate, then write through MindIE Bench `append_ledger_and_render_progress`."""
    from mindie_bench.runner import rsi_explorer
    # Own the two file writers so Windows CRT text-mode `os.open`/`fdopen`
    # cannot inject CR; judgment and PROGRESS.md rendering stay in Bench.
    rsi_explorer._append_json_line = _append_json_line
    rsi_explorer._atomic_write_text = _atomic_write_text
    entry = validate(record)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    progress = Path(progress) if progress else path.parent / "PROGRESS.md"
    with exclusive_lock(path):
        if entry.get("supersedes_exp_id") is None and any(
                r.get("task_id") == entry["task_id"] and r.get("exp_id") == entry["exp_id"]
                for r in read_ledger(path)):
            raise ValueError(f"exp_id {entry['exp_id']!r} already recorded; set "
                             "supersedes_exp_id to revise it")
        rsi_explorer.append_ledger_and_render_progress(
            path, progress, entry, campaign_id=entry["task_id"], goal=goal)
    return dict(status="recorded", exp_id=entry["exp_id"], verdict=entry["verdict"],
                progress=str(progress))


def fold(records) -> tuple[list[dict], dict]:
    """Apply `supersedes_exp_id` revisions in ledger order.

    Returns the live records (latest revision per experiment, in order) and the
    spend: distinct `run_ids`, and card-seconds per revision chain. A revision that
    adds no new run restates the same device time, so its `card_seconds` (when
    given) replaces the chain's value instead of adding to it.
    """
    live, chain_seconds, seen = {}, {}, set()
    for record in records:
        runs = set(record.get("run_ids") or ())
        fresh = bool(runs - seen)
        seen |= runs
        own = record.get("card_seconds")
        prior = record.get("supersedes_exp_id")
        carried = None
        if prior is not None and prior in live:
            live.pop(prior)
            carried = chain_seconds.pop(prior)
        if carried is None:
            seconds = float(own or 0)
        elif fresh:
            seconds = carried + float(own or 0)
        else:
            seconds = carried if own is None else float(own)
        key = record.get("exp_id")
        live.pop(key, None)
        live[key], chain_seconds[key] = record, seconds
    return list(live.values()), dict(runs=len(seen), card_seconds=sum(chain_seconds.values()))


def gate(path, *, task_id, source_digest, scope=None, max_runs=None, max_card_seconds=None):
    """Decide before a candidate goes to the device: submit, duplicate or budget_exhausted.

    Mechanism follows vllm-evolve `bench/eval_pool.py` (HashCache + EvalBudget).
    """
    live, spent = fold(r for r in read_ledger(path)
                       if r.get("task_id") == task_id and r.get("exp_id"))
    spent.update(max_runs=max_runs, max_card_seconds=max_card_seconds)
    for record in reversed(live):
        if record.get("source_digest") == source_digest and record.get("verdict") in SETTLED \
                and (record.get("conditions") or {}) == (scope or {}):
            return dict(decision="duplicate", spent=spent,
                        prior=dict(exp_id=record.get("exp_id"), verdict=record["verdict"],
                                   run_ids=record.get("run_ids")),
                        reason="this exact source was already judged under the same conditions")
    if max_runs is not None and spent["runs"] >= max_runs:
        return dict(decision="budget_exhausted", spent=spent,
                    reason=f"{spent['runs']} device runs recorded; budget is {max_runs}")
    if max_card_seconds is not None and spent["card_seconds"] >= max_card_seconds:
        return dict(decision="budget_exhausted", spent=spent,
                    reason=f"{spent['card_seconds']:g} card-seconds recorded; "
                           f"budget is {max_card_seconds:g}")
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
    write = commands.add_parser("append")
    write.add_argument("--ledger", required=True)
    write.add_argument("--progress")
    write.add_argument("--goal", default="")
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
            result = append(args.ledger, _stdin_json(), progress=args.progress, goal=args.goal)
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
