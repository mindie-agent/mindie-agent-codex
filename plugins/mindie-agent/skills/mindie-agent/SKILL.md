---
name: mindie-agent
description: "Ascend NPU, vLLM-Ascend, xLLM, MindIE-SD, AscendC, and Triton engineering workflow using out-of-the-box mindie-remote-dev and mindie-tools MCP servers (including L1-L4 graded knowledge)."
---

# MindIE Agent (Ascend NPU Engineering Workflow)

## 1. Silent Default-On Tooling & Task Continuity
- **Ready out of the box**: `mindie-remote-dev` (`remote_*`) and `mindie-tools` (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`, `coordinator`, `profiler`, `debugger`, `sim`, `diagnostics_status`, `diagnostics_bundle`, `rsi_loop`, `experiment_ledger`) work in CLI, Desktop, Work mode, Automations, and Subagents with zero session-activation gates.
- **Task continuity**: Helpers are optional. If any helper fails, report that specific error once and continue via deterministic MCP or host-native tools. Reconcile any `unknown` remote write before repeating it.
- **Physical NPU exclusivity**: Acquire a `coordinator` lease (or verify an existing host reservation) before running benchmarks, profiling, or workloads on physical NPUs; check existing `execution_id` / `job_id` handles before resubmitting.

## 2. Code-First Investigation & L1-L4 Knowledge Loop
- **Inspect code first**: Read target code and callers before querying knowledge; proceed directly when root cause and fix are clear.
- **Two-step retrieval**:
  1. `knowledge_query`: pass concise bilingual technical terms, optional `domain`, and observed `conditions` (`token_budget` default `8000`, up to `64000`). Probe missing facts whenever `match_status == "condition_unknown"`.
  2. `knowledge_explain`: pass revision-bound `mindie://<domain>/units/<id>@<revision>` URIs in `refs=[...]`, observed `conditions`, visible `known_refs=[...]`, and `token_budget` (default `16000`).
- **Feedback, distillation & iterative tuning**: Record `knowledge_feedback` on `feedback_ref` when helpful, and call `knowledge_distill` at `verified_milestone` or `completed` (`reproduced_empirical` requires repeated verified runs; `L4` requires leave-one-out attribution). Pass explicit `--time-window`, `--step-window`, or `--full-range` and `--clock-domain` to `mindie_profiler`, and use `rsi_loop` / `experiment_ledger` (plus `L4` exploration on `L3` plateau) for multi-round kernel or serving optimization.

## 3. Progressive References (Load Only When Needed)
- Tool schemas, profiler flags & experiment ledger: [Domain Tooling](references/domain-skills.md)
- Diagnostic isolation & scale-up verification (`W1-W7`): [Diagnostic Reasoning](references/diagnostic-reasoning.md)
- Session, opt-in capture lifecycle & historical import: [Capture & Operation Lifecycle](references/activation-lifecycle.md)
