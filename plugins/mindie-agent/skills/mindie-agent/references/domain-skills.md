# Domain Tooling, Graded Knowledge & Long-Horizon Execution

The `mindie-remote-dev` and `mindie-tools` MCP servers (including `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`, `coordinator`, `profiler`, `debugger`, `sim`, `diagnostics_status`, `diagnostics_bundle`, `rsi_loop`, and `experiment_ledger`) are enabled by default in every session with zero activation gate. Pass explicit host, port, user, container, and working directory parameters, and poll or manage returned job/execution handles directly.

## 1. Graded Knowledge (`L1–L4`), Reference Identity & `knowledge_distill`

- **Purpose Tier vs. Evidence Strength**:
  - `tier` (`L1` axioms & measurement probes, `L2` troubleshooting SOPs, `L3` convergence workflows, `L4` exploration mutators) describes how a unit is used.
  - `evidence_strength` (`hardware_spec` | `reproduced_empirical` | `single_observation` | `hypothesis`) and `provenance` describe how strongly it is supported.
- **`knowledge_query` (`graded-bm25`)**:
  - Parameters: `query`, optional `domain` (default domain or a registered `domain_configs` route such as `"vllm-ascend"` or `"operators"`), `limit` (`1..20`), `conditions`, `continuation`, `domain_path`, `token_budget` (`1..64000`, default `8000` in standard/reuse mode and `24000` in `mode="explore"`), `include_unknown` (default `false`), and `include_archived` (default `false`).
  - Returns bounded navigation `cards`, deduplicated `foundation_cards` (matched `L1` premises), compatible `results[]`, and `usage`.
  - Each item includes `id` (`unit-...`), revision-bound `ref` (`mindie://<domain>/units/<id>@<integer revision>`), `unit_ref` (`mindie://<domain>/units/<id>`), `feedback_ref` (equal to `ref` for graded units), `revision`, `match_status` (`matched` | `mismatched` | `condition_unknown`), `missing_conditions`, `tier`, `category`, `evidence_strength`, `domain_path`, `conditions`, `provenance`, `excerpt`, and `inherited_rules`.
- **`knowledge_explain` & `knowledge_feedback` Routing**:
  - Pass revision-bound `ref` URIs (`mindie://<domain>/units/<id>@<integer revision>`) to `knowledge_explain(refs=[...], conditions=..., known_refs=..., token_budget=16000)` (`1..64000`, default `16000` in reuse/diagnose and `24000` in `mode="explore"`) and `feedback_ref` to `knowledge_feedback`. Both route by the `mindie://<domain>/...` URI authority (do not pass bare IDs or mix domains in one batch).
  - `knowledge_explain(refs=[...])` returns complete untruncated bodies in `units`, shared prerequisites once in topological order in `requires`, and any exclusions in `unresolved` (`condition_unknown`, `condition_mismatch`, `budget_excluded`, `removed_or_superseded`, `not_found`). `known_refs` should list only exact `id@revision` refs currently visible in active context.
  - `knowledge_feedback` records four-stage usage events (`discovered` | `loaded` | `applied` | `evaluated`, with `run_id`, optional `step_id`, and `outcome="supported"|"refuted"|"inconclusive"`) and/or an optional `rating="up"|"down"`. When citing units in reports or ledgers, record `unit_id` and integer `revision`.
- **Three-Valued `conditions` (`matched` | `mismatched` | `condition_unknown`)**:
  - Query evaluates each unit's declared condition keys (`soc`, `cann`, `driver`, `framework`, `repository`, `revision`, `workload_scope`, `dtype`, `input_shape`, etc.). If a required premise or observed fact is missing or `"unknown"`, status is `condition_unknown`; probe the missing fact before applying the unit.
- **Selected-Evidence Distillation (`knowledge_distill`)**:
  - `knowledge_distill(inputs, task_state, domain=..., related_refs=[...], proposal=...)` distills authorized evidence (`inputs` items with `kind`, `outcome`, `source_locator`, `content`, `evidence_refs`, optional `repetitions >= 1`) at `task_state="verified_milestone"` or `"completed"`.
  - `hardware_spec` requires a `hardware_spec` source; `reproduced_empirical` requires `outcome == "verified"` with `repetitions >= 2`; `L4` requires verified leave-one-out factor attribution. `observed` or fault/build evidence (`compile_failed`, `infra_failed`, `measurement_invalid`) distills into `single_observation` or `hypothesis`.
  - Pass `related_refs` for delta comparison (`revise`/`merge`/`supersede` over duplicates), or pass a `mindie-distill-decision/1` `proposal` for deterministic validation and application (`model_calls=0`).

## 2. Stagnation Gate & De-Biased Exploration

- When three consecutive unimproved attempts on distinct candidate digests (`candidate_sha256`) occur on the same `L3` branch in `experiment-ledger.jsonl`, stop repeating that branch.
- Call `knowledge_query(query=..., conditions={...}, token_budget=24000, native_subcontext=True, bottleneck_summary={...}, goal=...)` using the default `mode="reuse"`; when `ledger_entries` show `L3` stagnation, the routing engine automatically switches `requested_mode="reuse" -> effective_mode="explore"` (`auto_switched_from_reuse=True`) and returns `exploration_context.subagent_fact_pack` (`stripped_l3_ids`).
- Optionally spawn a fresh native subagent seeded with `subagent_fact_pack.prompt_brief` (L1 hard constraints + L2 bottleneck + L4 mutators + `conditional_avoid`, with prior L3 trajectory stripped).

## 3. Optional Iterative Optimization Helpers (`mindie_harness.rsi`)

- **Handle-First Continuation**: `coordinator` (`execution_id`) and `remote-dev` (`job_id`) persist authoritative run state on disk. After any context compaction, interruption, or session resume, inspect existing handles (`coordinator` status/observe or `remote_job_status`) before launching duplicate runs.
- **Optional Experiment Ledger & Loop (`experiment_ledger.py` / `rsi_loop.py`)**:
  - Both `<runtime_scripts>/experiment_ledger.py` and `<runtime_scripts>/rsi_loop.py` forward to `mindie_harness.rsi` and remain strictly optional helpers for multi-round kernel or serving tuning.
  - `experiment_ledger.py` provides `fingerprint <repo>`, `judge` (paired-trial median ratio + multi-factor leave-one-out attribution check), and `append --ledger experiment-ledger.jsonl`. Compile errors, SSH drops, or invalid measurement windows are recorded as `inconclusive` rather than `refuted`.
  - For multi-factor candidates, verify individual factor necessity via sequential leave-one-out pruning where the retained combination passes both correctness and performance checks.
  - `rsi_loop.py` (`init`, `next`, `complete <step>`, `finish`) coordinates optional `profile -> consult -> propose -> measure -> learn` rounds over `experiment-ledger.jsonl`.

## 4. NPU Timing Mutual Exclusion & Profiler Discipline

- **CPU Pre-Compilation (`npu_count=0`)**: Confirmed device-free build or inspection steps may run concurrently using per-slot cache directories (`MINDIE_SLOT_ID`, `ASCEND_CACHE_PATH`, `TORCHINDUCTOR_CACHE_DIR`, `TRITON_CACHE_DIR`).
- **Exclusive Physical NPU Lease**: Any benchmark, attribution check, or `mindie_profiler` capture on the same physical NPU (or same Ascend A3 dual-Die package, `Phy-ID 0..15`) must hold an exclusive `coordinator` lease or an equivalent verified host reservation and drain `/proc` process trees before the next run.
- **`mindie_profiler` Conventions**:
  - Pass explicit `--time-window START:END`, `--step-window STEP=START:END` / `--steps-file`, or `--full-range` (`--unit us|ns|ms`), plus explicit scene annotations (`--mode`, `--framework`, `--phase`, `--tp`, `--dp`, `--graph-mode`, `--clock-domain`, `--input-signature`).
  - `busy_union` and `kernel_sum` are non-additive (`host_gap = wall - busy_union`, `exposed_comm = comm_union - noncomm_union`), and `slowest_rank` is selected by wall `p50` across 7 semantic blocks (`Attention`, `MoE`, `Comm`, `Norm`, `Copy_Layout`, `Host_Gap`, `Unclassified`).
  - Keep `ideal_hardware_lower_bound_ms` (single-kernel Roofline bound), `model_predicted_ms`, and `measured_device_ms` distinct, and rely on explicit hardware throttle signal bits rather than a static `85°C` threshold.
