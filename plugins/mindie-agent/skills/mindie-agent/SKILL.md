---
name: mindie-agent
description: Manually enable MindIE knowledge, domain skills, feedback, distillation and background capture for the current session through /mindie-agent or $mindie-agent. Configured deterministic tools remain available without this entry.
---

# MindIE Agent (Unified Entry)

Use this skill when the user explicitly invokes `/mindie-agent` or `$mindie-agent` in the current Codex session. The `UserPromptSubmit` hook grants session access on that command.

Before your first knowledge call in the session, resolve the active adapter configuration and run `python -I <runtime_scripts>/bridge.py activate` once in the native shell (which supplies `CODEX_THREAD_ID`). That command activates the session grant and prepares eligible background capture; new chats, forks, and subagents require their own entry.

Knowledge tools are invoked via `knowledge_cli.py`:
- Run `python -I <runtime_scripts>/knowledge_cli.py describe` when you need the tool schemas.
- Run `python -I <runtime_scripts>/knowledge_cli.py <tool_name>` with JSON arguments on stdin (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`).

Deterministic tools (`remote-dev`, `coordinator`, `debugger`, `profiler`, `sim`, `diagnostics_status`, `diagnostics_bundle`) are always available in every session without activating this skill.

## On-Demand Knowledge Retrieval

- Inspect the relevant code and callers first. If the root cause and fix are already clear from the codebase, proceed directly without querying knowledge.
- Probe missing environment or runtime facts before querying, and query knowledge when a domain contract, hardware constraint, or troubleshooting workflow can inform your next step.
- Reuse complete rules already visible in context. To read graded units (`L1–L4`), pass `refs` (even for a single unit), observed `conditions`, visible `known_refs`, and `token_budget` on stdin to `knowledge_cli.py knowledge_explain`, copying the exact `mindie://<domain>/units/<id>@<revision>` URIs.
- Once the next step and its prerequisites are clear, stop reading and execute. Re-query if new evidence or verification results change the picture.

## Core Tools & Operational Contracts

- **Domain Knowledge (`L1–L4` Graded Units, Multi-Domain Routing & Three-Valued `conditions`)**:
  - `knowledge_query`: searches the default or selected `domain` (`query`, optional `domain`, `limit` 1–20, `conditions`, `continuation`, `domain_path`, `token_budget` 1–16000, `include_unknown`, `include_archived`). Returns navigation `cards`, deduplicated `foundation_cards` (`L1` premises), and `usage`.
  - `knowledge_explain(ref=... | refs=[...], conditions=..., known_refs=..., token_budget=4096)`: expands full bodies and shared `requires` prerequisites in topological order without truncating unit content. `known_refs` should list only exact `id@revision` refs still fully visible in active context (re-read after context compaction). Always pass full `mindie://<domain>/...` URIs rather than bare IDs, and keep all refs in a batch within the same domain authority.
  - `knowledge_feedback`: records usage events (`event_type`: `discovered` | `loaded` | `applied` | `evaluated` with `run_id`, optional `step_id`, and `outcome`: `supported` | `refuted` | `inconclusive`) and/or an optional `rating` (`up` | `down`) using the exact `feedback_ref`.
  - `knowledge_distill(inputs, task_state, domain=..., related_refs=[...], proposal=...)`: distills selected, authorized evidence into local graded units when `task_state` is `"verified_milestone"` or `"completed"`:
    - `observed` or fault/build evidence (`compile_failed`, `infra_failed`, `measurement_invalid`) can be distilled as `single_observation` or `hypothesis`; `reproduced_empirical` requires repeated `verified` evidence (`repetitions >= 2`), and `L4` requires a verified ablation receipt.
    - Pass `related_refs` to revise/merge against existing units as a delta, or pass a `mindie-distill-decision/1` `proposal` for deterministic validation and application (`model_calls=0`).
  - **Three-Valued `conditions` (`matched` | `mismatched` | `condition_unknown`)**: Graded units separate functional `tier` (`L1` axioms/probes, `L2` SOPs, `L3` convergence workflows, `L4` mutators) from `evidence_strength` (`hardware_spec`, `reproduced_empirical`, `single_observation`, `hypothesis`). When a unit's required premise or the caller's observed fact is missing or `"unknown"`, status is `condition_unknown` (`ready_to_apply=false`); probe the missing fact before applying the rule.
- **Remote Ascend NPU Development (`mindie-remote-dev`) & Long-Horizon Execution**:
  - **Launch-Time `PROGRESS.md` Registration & Recovery**: Whenever `coordinator` or `remote-dev` starts a managed run or background job, atomically update `PROGRESS.md` to record its `run_id` (`run_id == execution_id`) or `job_id`, `slot_id`, endpoint, and `Run Manifest` path under `in_flight_runs` before waiting or ending the turn. After any interruption, compaction, or resume, check `in_flight_runs` first rather than submitting a duplicate run. Record condition-scoped hypothesis outcomes in `experiment-ledger.jsonl`, and treat `candidate_for_sequential_pruning` with `pruned_combination_verified=false` as **RETAIN**.
  - **NPU Lease & Profiler Discipline**: Use `npu_count=0` only for steps confirmed to need no device. Device JIT, warmup, performance timing, attribution checks, and `mindie_profiler` runs on the same physical NPU (or same A3 dual-Die package) require an exclusive `coordinator` lease. Pass explicit `--time-window START:END`, `--step-window STEP=START:END`, or `--full-range` to `mindie_profiler`, and supply explicit scene and `--clock-domain` annotations.
- **Unified Contribution & Fault-Reporting Switch**:
  - Community knowledge sharing and sanitized tool fault reporting share one persistent setting in `<adapter-config-directory>/mindie-consent.json`. Reuse valid saved settings (`contribute`, `disabled`, `read-only`, `later`) without prompting again. If a legacy knowledge-only `contribute` record returns `needs_choice`, resolve it once through `/mindie-agent`. Keep three states distinct when reporting status: (1) local consent config saved, (2) background reporter running, and (3) external GitHub Issue/PR created.

## Diagnosis, Exploration & De-Biased Subagents

- **Targeted experiments**: Choose the lowest-cost experiment that separates competing hypotheses. Judge each run across three independent axes: `Performance` (measured delta vs. baseline/target), `Correctness/Invariants` (numerical/semantic validity), and `Diagnostic Information Gain` (new discriminating facts or narrowed conditions).
- **Stagnation gate & de-biased exploration**: When `assess_stagnation` reports three consecutive unimproved attempts on the same L3 route (deduplicated by `run_id` with no new fact or divergent layer), call `knowledge_query(query=..., mode="explore", native_subcontext=True, ledger_entries=[...], bottleneck_summary={...}, goal=...)`. This strips prior L3 workflows, preserves L1 foundations, and returns `exploration_context.subagent_fact_pack` (`schema_version == "mindie-rsi-subagent-pack/1"`). Spawn a fresh native subagent seeded only with `subagent_fact_pack.prompt_brief` (L1 hard constraints + L2 bottleneck evidence + L4 mutators + `conditional_avoid`).

For first-divergence checks, observer effects, stateful reproducers, independent oracles, progressive scale-up verification (`W1–W7`), and `C1–C5` contracts, read [Diagnostic Reasoning](references/diagnostic-reasoning.md).

## Progressive Reference Loading (Read on Demand)

Load reference files only when needed for the current step:

- **First-use GitHub authentication, enterprise-network token login, or auth troubleshooting**: [GitHub Sign-In](references/github-authentication.md).
- **First-use onboarding and workspace setup**: present [用户须知](references/user-notice.md), then follow [Workspace Setup](references/workspace-setup.md).
- **Detailed tool schemas (`knowledge_query`, `knowledge_explain`, `knowledge_distill`, `remote_*`), graded response fields, `PROGRESS.md` + `experiment-ledger.jsonl`, and profiler rules**: [Domain Tooling](references/domain-skills.md).
- **Configuration, status diagnosis (`entry.py bridge`), `Stop` capture lifecycle, or explicit historical transcript import (`history-import`)**: [Capture & Operation Lifecycle](references/activation-lifecycle.md).
- **Sharing scope, local redaction rules, or public Git history questions**: [用户须知](references/user-notice.md).
