---
name: vllm-ascend-performance-regression
description: "Run or analyze controlled baseline-versus-candidate vLLM Ascend serving experiments to judge throughput, latency, startup-time or HBM regressions."
---

# vllm-ascend-performance-regression

Run or analyze a controlled baseline-versus-candidate serving experiment and apply metric-specific regression thresholds.

Choose the workload and metrics that reflect the user-visible change. Set threshold and direction per metric. Keep non-code conditions comparable and inspect variance, outliers and failed requests before attributing a delta to code.

## Agent entry

For managed execution, use the [MindIE entry](../mindie-agent/SKILL.md) to activate this task once and pass its credentials to the CLI. An existing active lease is reused; an expired or paused lease needs another explicit invocation. Local evidence-only reports do not start the service.

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/vllm-ascend-performance-regression/scripts/performance_regression.py --config experiment.json
```

The business config names baseline.sources and candidate.sources (actual vllm and vllm-ascend worktrees), benchmark options, runs, warmups and thresholds. The collector passes each source selection to that run without changing task defaults, waits for its managed service, warms each launch, alternates A/B order, records runtime observations, and releases owned executions. --results accepts existing measurement files for report-only use. Missing runtime evidence yields an inconclusive report.

Preparation and release use bounded coordinator waits on the same execution.
The collector accepts compact serving receipts and performs business readiness
through the shared serving probe; no client-side status polling is required.

For a single-state throughput measurement use benchmark. For root-cause timing attribution use profiling collection and analysis.

Read the relevant detail only when needed:

- [Experiment input example](references/inputs.md)
