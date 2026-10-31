---
name: mindie-agent
description: Manually enable MindIE knowledge, domain skills, feedback, distillation and background capture for the current session through /mindie-agent or $mindie-agent. Configured deterministic tools remain available without this entry.
---

# MindIE Agent (Unified Entry)

Use this skill only after the user explicitly invokes `/mindie-agent` or
`$mindie-agent` in the current native task. Do not activate it from topic matching.
The native UserPromptSubmit hook grants session access on that manual gesture.
Before the first knowledge call, resolve the active adapter configuration and
run its configured `python -I <runtime_scripts>/bridge.py activate` once in the
native shell. The shell supplies `CODEX_THREAD_ID`; never invent task identity.
This reuses the native grant and prepares eligible capture. Check the returned
activation result before querying. Reuse that task's grant
until explicit deactivation; new chats, forks and subagents need their own entry.

Knowledge tools are absent from default MCP discovery. When a knowledge call is needed after activation, run
the configured `python -I <runtime_scripts>/knowledge_cli.py describe` to load
their schemas. Execute a knowledge operation with
`python -I <runtime_scripts>/knowledge_cli.py <tool_name>`, passing its JSON
arguments on stdin. This uses the same domain engine and native session grant;
it does not register knowledge tools in a shared MCP connection. Preserve the
returned `isError` and nonzero exit status. Ordinary tasks must not load this
private catalog.

The manual entry gates the entire knowledge flywheel: query, explain, feedback,
distillation, knowledge sync and background `Stop` capture. Capture additionally
requires the saved contribution choice and approved project scope. Installation,
consent, discovery and ordinary prompts never activate knowledge or capture.
Reuse saved contribution choices without asking again.

The manual grant controls knowledge and experience context only. Configured
deterministic tools, including resource probes/leases, profiling,
simulation calculations, local diagnostics and factual status checks, remain
available independently. Call their ordinary tool/CLI interfaces directly;
they do not require loading this knowledge skill.

## Retrieve for a missing decision

Use the current Codex turn to choose the next action. Manual entry permits
knowledge use; a self-contained repair can finish with zero knowledge calls.

- Read the implicated code and necessary callers. If they explain the violated
  invariant and support a fix and verification, proceed directly; a filename or
  stack frame alone is insufficient.
- Probe missing environment or execution facts with the available tools. Load
  the private knowledge catalog only when a missing contract, mechanism or
  method could change the next action. Do not invent applicability conditions.
- Reuse complete rules still visible. A previously returned exact ref needs no
  new query. For a needed graded read, pass JSON `refs` (even one unit), observed
  `conditions`, visible `known_refs` and `token_budget` on stdin to the configured
  `knowledge_cli.py knowledge_explain`; copy revision references exactly.
- Once the next action and its required premises are clear, stop reading and
  act. New evidence or failed verification can reopen retrieval. Preserve the
  task's correctness, device and acceptance checks. Skipping a read needs no
  feedback call and does not revoke the grant or eligible background capture.

## Finish the verified task

Identify the requested behavior and its acceptance checks from the task and
relevant callers. Once the change satisfies those invariants and the affected
behavior checks pass, give the final response with the result, validation and
any remaining limitation. Let the host emit its normal completion event.

Extend the implementation or repeat validation only for a concrete failing
case, a required acceptance check, or new evidence that challenges the result.
Do not add optional parameter combinations, speculative compatibility paths or
defensive fallbacks after success without that evidence. An offline parser or
code repair does not require an invented hardware condition or an unrelated
live NPU run. Keep any hardware checks the actual task requires.

Knowledge feedback and background capture must not prolong a finished task.
A passing check alone does not fabricate native completion or C6 admission;
report unfinished requirements honestly if a budget or external dependency
prevents completion.

## Core Tools in Everyday Tasks

- **Domain Knowledge (private manual entry — Graded `L1–L4`, Multi-Domain Routing & Three-Valued `conditions`)**:
  - `knowledge_query`: search the default or explicitly selected `domain` (`query`, optional `domain`, `limit` 1–20, `continuation`, plus graded parameters `domain_path`, `token_budget` 1–16000, `include_unknown`, `include_archived`). Returns bounded navigation `cards`, deduplicated `foundation_cards`, and `usage`; unknown conditions stay `match_status="condition_unknown"` with `ready_to_apply=false`, and archived/superseded units are excluded unless `include_archived=true`.
  - `knowledge_explain(ref=... | refs=[...], conditions=..., known_refs=..., token_budget=4096)`: read query's readable `ref` (`mindie://<domain>/units/<id>@<integer revision>`) for a `graded_unit` (which also returns `feedback_ref`), or a task/block `ref` for legacy block navigation. Batch `refs` assembles the dependency closure (`units` + `requires`) minus exact `known_refs`, expanding each shared premise once and never truncating a body; `unresolved` reports `condition_unknown`, `condition_mismatch`, `budget_excluded`, `removed_or_superseded`, and `not_found`. Routing uses the `mindie://<domain>/...` URI authority (for graded units `ref` and `feedback_ref` share the same revision-bound URI value, whereas legacy material keeps `ref` and `feedback_ref` distinct; never pass a bare `id`; a batch must not mix domain authorities).
  - `knowledge_feedback`: optionally record `up` or `down` by copying the exact returned `feedback_ref` (`mindie://<domain>/...`, routed by URI authority domain, including revision-bound `/units/<id>@<integer revision>` for graded units; never pass a bare `id`). In the same call, or as an event-only call, record the four-stage usage flywheel with optional `event_type` (`discovered` | `loaded` | `applied` | `evaluated`), `run_id`, `step_id`, and, for `evaluated`, `outcome` (`supported` | `refuted` | `inconclusive`).
  - `knowledge_distill(inputs, task_state, domain=..., related_refs=[...], proposal=...)`: explicitly distill caller-selected, already-authorized evidence into local C5 graded units for the default or activated target `domain` when `task_state` is `"verified_milestone"` or `"completed"`:
    - Completed or milestone runs with `observed` or fault/environment/build evidence (`compile_failed`, `infra_failed`, `measurement_invalid`) can yield `single_observation` or `hypothesis` units while preserving their true evidence strength (never elevating unverified faults to `hardware_spec` or `reproduced_empirical`; `reproduced_empirical` requires repeated `verified` evidence and `L4` requires a valid ablation receipt).
    - Pass `related_refs` to compare against same-topic units for delta updates, or pass an already-formed `mindie-distill-decision/1` `proposal` for deterministic validation with zero model calls (`model_calls=0`).
    - Calling with `task_state="in_flight"` and no verified milestone (or empty inputs) is a zero-call no-op; a single-turn host `Stop` event is **not** task completion.
  - **Three-Valued `conditions` (`matched` | `mismatched` | `condition_unknown`) & Paired-Runtime Boundary**: Knowledge is reference material, never blind instructions. Graded units separate functional `tier` (`L1` axioms/probes, `L2` SOPs, `L3` convergence workflows, `L4` mutators) from `evidence_strength` (`hardware_spec`, `reproduced_empirical`, `single_observation`, `hypothesis`) and evaluate declared condition keys:
    - `condition_unknown` occurs when either the unit's `required` premise or the caller's `observed` fact is `"unknown"`, missing, or incomparable (`unknown == unknown` never matches, whereas an omitted requirement or `"any"` is unconstrained `matched`, and historical `required: "unknown"` is never backfilled). Never treat `condition_unknown` as a match—probe the missing fact first (e.g., source `repository`/`revision` for an `L2` SOP, or runtime environment for an NPU rule).
    - Graded query options, multi-domain routing, `knowledge_distill`, `mindie-publication-contract/2`, independent Feed sync, and revision-bound unit feedback require the paired `mindie-knowledge 0.13.0` runtime. When diagnosing a mismatch, check the selected feed revision and installed runtime identity separately.
- **Remote Ascend NPU Development (`mindie-remote-dev` MCP) & Long-Horizon Execution**:
  - Available automatically on demand in every native task for remote files, commands, jobs, and artifacts without activating knowledge capture.
  - **Long-Horizon Continuation & Recovery**: Immediately when `coordinator` or `remote-dev` starts a managed run or job, atomically update `PROGRESS.md` to register its `run_id` (`run_id == execution_id`, never replaced) or `job_id`, `slot_id`, endpoint, and `Run Manifest` path under `in_flight_runs` before waiting or ending the turn. After any interruption, compaction, or resume, query `in_flight_runs` first and never blindly resubmit an active or uncertain run. Keep `Run Manifest` execution facts separate from condition-scoped hypothesis judgments in `experiment-ledger.jsonl`, and treat `candidate_for_sequential_pruning` with `pruned_combination_verified=false` as **RETAIN**.
  - **Pre-Compilation Parallelism, Physical NPU Timing Mutual Exclusion & Profiler Discipline**: Only steps confirmed to need no device may use `npu_count=0` and parallel slot caches. Device JIT and warmup require the actual device lease; performance timing, attribution checks, and `mindie_profiler` runs on the same physical NPU (or same A3 dual-Die package) require an exclusive `coordinator` lease. `mindie_profiler` accepts explicit unlabeled `--time-window START:END`, labeled `--step-window STEP=START:END`, or `--full-range` (never inferring steps from kernel rows), uses explicit scene/clock annotations (`uncertain` / `clock_domain_unknown` otherwise), keeps single-kernel roofline bounds separate from unverified external `msmodeling` predictions, and never infers thermal throttling from a static `85°C` threshold.
- **Diagnostics & Unified Contribution Switch**:
  - Natural capability results can include bounded `agent_diagnostics` references for the Agent. Preserve the actual business result and report only limitations relevant to the user's goal. Stop capture must never request another model turn or block task completion.
  - Community knowledge sharing and automatic tool fault reporting share a single persistent choice in `<adapter-config-directory>/mindie-consent.json`. Reuse a valid saved choice without another prompt. Only when the entry returns `needs_choice` for a legacy knowledge-only `contribute` choice, resolve it once through `/mindie-agent` using the user's explicit choice; do not ask separately for knowledge and fault reporting, or ask again in each task. Keep saved `disabled`, `read-only` and `later` choices without prompting. Always keep three states distinct: (1) local consent configuration saved, (2) background reporter worker running, and (3) external GitHub Issue/PR published.

The consent file is beside the active adapter configuration (`adapter_config.parent / "mindie-consent.json"`); use the installed entry to change the choice, never a guessed profile path.

## Knowledge use, diagnosis and exploration

Knowledge tiers (L1 constraints/measurements, L2 procedures, L3 decision branches,
L4 transformations), evidence strength and the current work mode are independent.
Hermes is experience revision across tiers; exploration does not require an L4 result.

- **Apply relevant knowledge when needed**: follow the missing-decision policy above. Read complete selected bodies and required premises before adopting them; probe unknown facts and choose L3 branches from current observations.
- **Explore an unresolved subproblem**: choose the least costly experiment that separates competing explanations. New facts or a first divergence count as progress even if the full test fails. Repeated no-information attempts prompt reconsideration, not an automatic mode switch. Use native independent context only when available and authorized; filtering an object or inheriting full history does not establish isolation. Device-free work may use `npu_count=0`; JIT, warmup and measurement use the required lease and actually assigned devices.
- **Revise from bounded evidence**: use `task_state="verified_milestone"` for a finished finding in an ongoing task, or `"completed"` for completion. Preserve counterevidence, human corrections, superseded plans and unresolved alternatives; compare related units and prefer revision or no-op. Separate evidence validity, goal attainment and reusable information, with supported/refuted/inconclusive feedback. L4 transformations still require a verified final-combination ablation receipt; diagnostic L2/L3 findings do not require performance gains.
- **Stagnation gate**: when `assess_stagnation` reports three consecutive unimproved attempts (deduplicated by `run_id`, with no new fact or identified divergent layer), stop varying the current L3 route and escalate instead of spending another near-identical attempt.
- **Native de-biased exploration**: call `knowledge_query(query=..., mode="explore", native_subcontext=True, ledger_entries=[...], bottleneck_summary={...}, goal=...)`; the result strips L3 workflow units, keeps L1 foundations, and returns `exploration_context.subagent_fact_pack`.
- **Seed only from the pack**: spawn a fresh native subagent/native subcontext seeded ONLY with `subagent_fact_pack.prompt_brief` (L1 hard constraints + L2 bottleneck evidence + L4 mutators + `conditional_avoid`, with the prior L3 trajectory removed); never inherit or paste the old conversation as isolation.
- **Verify real isolation**: require `subagent_fact_pack.schema_version == "mindie-rsi-subagent-pack/1"` and inspect `stripped_l3_ids`; when `native_subcontext` is false the pack is an in-session warning, not a real isolation claim.

For first-divergence checks, observer effects, history-dependent reproducers,
independent oracles, progressive scale-up verification (pruned vs. full weights),
same-topology baseline A/B parity, speculative graph/SLA sweeps, and parent/child
PR delivery workflows, read [Diagnostic Reasoning](references/diagnostic-reasoning.md).

## Progressive Knowledge Loading & Experience Flywheel (`P0`–`P3`)

- **Loading when needed (`P0`)**: query only for a missing reference; use `cards` to select relevant candidates and `foundation_cards` to identify their premises. An already returned exact reference needs no discovery call. Read chosen graded units, including a single unit, through the bounded closure path: `knowledge_explain(refs=[...], conditions=..., known_refs=[...], token_budget=4096)`. `known_refs` are exact `id@revision` refs still fully visible in the active context—not everything ever read; after compaction, drop summarized refs and re-read as needed. `requires` contains shared prerequisites expanded once, and `unresolved` is explicit (`budget_excluded` or a condition gap), never a silently truncated body.
- **Milestone/delta/proposal distillation (`P1`/`P2`)**: at a verified milestone pass `task_state="verified_milestone"` with `related_refs` naming same-topic old units so the organizer produces a delta (prefer revise/merge/supersede over duplicate new units). When a `mindie-distill-decision/1` proposal has already been formed, pass `proposal` for deterministic validation and application (`model_calls=0`). Use P2 contraction (merge/replace/archive) in the proposal instead of creating endless near-duplicates.
- **Four-stage usage feedback (`P1`)**: `discovered` is delivery by query, `loaded` is a full-body read by explain, `applied` means the knowledge was actually used in a run (`run_id`, optional `step_id`), and `evaluated` binds `outcome` (`supported` | `refuted` | `inconclusive`). These are usage events, not quality votes; a `rating` may accompany them in the same call. Idempotent event identity prevents polling or replay from inflating counts.
- **Three-axis exploration (`P3`)**: judge each experiment independently on `Performance` (measured effect against a valid baseline/target; a valid sub-target gain is not a refutation), `Correctness/Invariants` (compile/oracle/invariant validity; a failure rejects the implementation, not necessarily the direction), and `Diagnostic Information Gain` (did the attempt produce a new discriminating fact, narrowed condition, or explained mechanism?). No new fact is `inconclusive`/information stagnation, not domain `refuted`; feed verified milestone results back as the appropriate L1/L2/L3/L4 unit.

## Progressive Context Disclosure (Read Only on Demand)

- **Installation, first-use GitHub authentication, enterprise-network token login or authentication repair**: read [GitHub Sign-In](references/github-authentication.md). Reuse a working login; otherwise guide browser login or token creation/input and verify credential reuse.
- **Normal installation and first-use setup, including when contribution is disabled**: present the existing [user notice](references/user-notice.md), then complete [Workspace Setup](references/workspace-setup.md). Offer creation/initialization, reuse or the current directory; approved creation/reuse includes native workspace registration. Reuse prior choices, and do not repeat this flow during ordinary tasks.

Do **not** load all reference files during ordinary troubleshooting. Read the specific reference file **only** when the current turn requires it:

- **Detailed tool schemas (`knowledge_query`, `knowledge_explain`, `knowledge_distill`, `remote_*`), graded response shapes, dual-layer ledger (`PROGRESS.md` + `experiment-ledger.jsonl`), and NPU timing / profiler rules**: read [Domain Tooling](references/domain-skills.md).
- **Explicitly requested configuration, status diagnosis (`entry.py bridge`), `Stop` vs. long-horizon continuation lifecycle, or importing past historical transcripts (`history-import`)**: read [Capture & Operation Lifecycle](references/activation-lifecycle.md).
- **Sharing scope, local redaction boundaries, or public Git history questions**: read [用户须知](references/user-notice.md).

