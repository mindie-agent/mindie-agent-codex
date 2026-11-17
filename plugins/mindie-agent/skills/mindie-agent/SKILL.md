---
name: mindie-agent
description: Manually enable MindIE knowledge, domain skills, feedback, distillation and background capture for the current session through /mindie-agent or $mindie-agent. Configured deterministic tools remain available without this entry.
disable-model-invocation: true
---

# MindIE Agent (Codex Unified Entry)

## 1. Core Operating Stance

- **Keep the user's task moving**: Knowledge, diagnostics, and local ledgers are optional helpers. If any helper is unavailable, report that specific failure once and continue with deterministic MCP tools or authorized host-native tools. Reconcile any `unknown` remote write before repeating it.
- **Zero-gate deterministic tools**: `remote-dev`, `coordinator` (`hosts`, `probe`, `container`, `lease`, and `task session|run|execution|finish` addressed directly by `execution_id`), `profiler`, `sim`, `debugger`, and local `diagnostics` work in every session (CLI, Desktop, Work mode, Automations, and Subagents) without `/mindie-agent`, `x-codex-turn-metadata`, or `context_file`. Acquire a `coordinator` lease (or verify an existing host reservation) before running workloads on physical NPUs; if device ownership cannot be confirmed, pause only that device step.
- **Explicit `/mindie-agent` knowledge gate**: Invoke this skill only when the user explicitly enters `/mindie-agent` or `$mindie-agent` in the current session (`UserPromptSubmit` grants the session lease; new chats, forks, and subagents require their own entry). Before your first knowledge call in the session, run `python -I <runtime_scripts>/bridge.py activate` once in the native shell (which supplies `CODEX_THREAD_ID`). If `activate` reports `Inactive`, surface the stage reason once without asking the user to repeat `/mindie-agent`, and continue the task directly.

## 2. Advisory Knowledge & Domain Tooling

Knowledge tools run through `knowledge_cli.py` (`python -I <runtime_scripts>/knowledge_cli.py describe` for schemas; pass UTF-8 JSON via `--input-file <path>` or stdin):

- **Inspect code first**: Read the relevant code and callers before querying knowledge. If the root cause and fix are already clear, proceed directly.
- **Two-step retrieval (`L1–L4` graded units)**:
  1. `knowledge_query`: pass concise bilingual technical terms, optional `domain`, and observed `conditions` (`token_budget` default `8000`, up to `64000`). Treat `condition_unknown` as unproven—probe the missing environment/source fact before applying the rule.
  2. `knowledge_explain`: pass exact `mindie://<domain>/units/<id>@<revision>` URIs in `refs=[...]`, observed `conditions`, currently visible `known_refs=[...]`, and `token_budget` (default `16000`).
- **Optional feedback & distillation**: Record `knowledge_feedback` (`discovered` | `loaded` | `applied` | `evaluated` or `up`/`down` on `feedback_ref`) when helpful, and call `knowledge_distill` at `task_state="verified_milestone"` or `"completed"` (`reproduced_empirical` requires repeated verified runs; `L4` requires verified leave-one-out factor attribution).
- **Long-horizon runs & de-biased exploration**:
  - `coordinator` (`execution_id`) and `remote-dev` (`job_id`) persist authoritative run state—check existing handles before resubmitting after an interruption or compaction. Pass explicit `--time-window`, `--step-window`, or `--full-range` and `--clock-domain` to `mindie_profiler`.
  - For multi-round kernel/serving optimization, you may optionally track hypotheses with `<runtime_scripts>/rsi_loop.py` (`experiment-ledger.jsonl`). When repeated `L3` repairs plateau on a structural bottleneck, query `L4` structural mutators (or let `assess_stagnation` switch to exploration) and optionally spawn a clean subagent seeded with `exploration_context.subagent_fact_pack.prompt_brief`, verifying multi-factor changes via leave-one-out ablations on the target operator's native interface.

## 3. Progressive References (Load Only When Needed)

- Detailed schemas, profiler flags & optional experiment ledger: [Domain Tooling](references/domain-skills.md)
- Diagnostic isolation & scale-up verification (`W1–W7`): [Diagnostic Reasoning](references/diagnostic-reasoning.md)
- Session/capture lifecycle & historical import: [Capture & Operation Lifecycle](references/activation-lifecycle.md)
