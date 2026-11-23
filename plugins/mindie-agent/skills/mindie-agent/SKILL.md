---
name: mindie-agent
description: "Ascend NPU, vLLM-Ascend, xLLM, MindIE-SD, AscendC, and Triton engineering workflow using out-of-the-box mindie-remote-dev (10-tool codex profile) and mindie-tools MCP servers, including L1-L4 graded knowledge and the read-only mindie_doctor health check."
---

# MindIE Agent (Ascend NPU Engineering Workflow)

## 1. Non-Blocking Task Continuity Rule
- **MindIE tools are accelerators, never roadblocks.** `remote-dev`, `coordinator`, `knowledge`, and `mindie_doctor` are optional helpers; the task must continue even when one is unavailable.
- If any tool fails, report the concrete error once and continue immediately through native `ssh`/`scp` or task-local scripts. Before repeating an uncertain remote write, reconcile its actual state first; never silently retry.
- `mindie_doctor` is a read-only readiness check (runtime, config, consent, SSH/NPU probe). Treat its failure as a diagnostic fact, not as permission to stop the user's task.

## 2. Silent Default-On Tooling
- `mindie-remote-dev` exposes the curated 10-tool Codex profile: `remote_exec_command`, `remote_write_stdin`, `remote_view_image`, `remote_apply_patch`, `remote_auth`, `remote_probe`, `remote_artifact_pull`, `remote_artifact_push`, `remote_job_status`, and `remote_job_stop`.
- `mindie-tools` exposes `mindie_doctor`, `mindie_status`, `diagnostics_status`, `diagnostics_bundle`, `profiler`, `sim`, `coordinator`, `debugger`, `rsi_plan`, `rsi_autotune`, `rsi_loop`, `experiment_ledger`, and the five `knowledge_*` tools.
- Privacy modes are explicit: `contribute` (community PRs), `local-only` (local capture only, zero GitHub upload, no `gh` required), and `read-only` (no capture).
- Physical NPU exclusivity: acquire a `coordinator` lease (or verify an existing reservation) before benchmarks, profiling, or workloads; check existing `execution_id` / `job_id` handles before resubmitting.

## 3. Code-First Investigation & L1-L4 Knowledge Loop
- Inspect target code and callers before querying knowledge; proceed directly when the root cause and fix are clear.
- Two-step retrieval:
  1. `knowledge_query`: concise bilingual technical terms, optional `domain`, observed `conditions` (`token_budget` 8000 by default, up to 64000). Probe missing facts when `match_status == "condition_unknown"`.
  2. `knowledge_explain`: revision-bound `mindie://<domain>/units/<id>@<revision>` refs, observed `conditions`, visible `known_refs`, and `token_budget` (16000 by default).
- Record `knowledge_feedback` when helpful; call `knowledge_distill` at `verified_milestone` or `completed`. L4 requires leave-one-out attribution.
- Use `rsi_loop` / `experiment_ledger` for multi-round kernel or serving optimization; prefer explicit `--time-window`, `--step-window`, or `--full-range` and `--clock-domain` for profiling.

## 4. Progressive References (Load Only When Needed)
- Tool schemas, profiler flags, and experiment ledger: [Domain Tooling](references/domain-skills.md)
- Diagnostic isolation and scale-up verification (`W1-W7`): [Diagnostic Reasoning](references/diagnostic-reasoning.md)
- Session, sharing, `mindie_doctor`, and SSH/NPU setup: [Capture & Operation Lifecycle](references/activation-lifecycle.md)
- Natural-language RSI planning and bounded autonomous iteration via `mindie-harness.rsi`: [RSI Natural Language Contract](references/rsi-natural-language.md)
