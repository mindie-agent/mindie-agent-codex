---
name: vllm-ascend-correctness-validation
description: "Compare vLLM Ascend inference outputs or AISBench accuracy across code states, execution modes or serving configurations. Use for numerical and token regression checks."
---

# vllm-ascend-correctness-validation

Compare inference outputs across code or execution configurations with explicit comparability and numerical criteria.

Select deterministic prompts or token IDs, sampling, model and topology that exercise the change. Token equality and dataset task metrics answer different questions. Declare only the intended varying dimensions with --allowed-difference.

## Agent entry

For managed execution, use the [MindIE entry](../mindie-agent/SKILL.md) to activate this task once and pass its credentials to the CLI. An existing active lease is reused; an expired or paused lease needs another explicit invocation. Local evidence-only reports do not start the service.

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py --cases cases.json --baseline baseline.json --candidate candidate.json
```

The remote_correctness_harness.py payload captures offline runtime observations from the managed execution. Online/AISBench results use the server execution reference through aisbench_adapter.py. The comparison derives metadata from actual outputs, emits its certificate and report, and reports missing identity as inconclusive.

Route an eager-passes/graph-fails reproduction to graph-debug, a rank-dependent failure to distributed-debug, and a reduced operator failure to operator-debug.

Read the relevant detail only when needed:

- [aisbench](references/aisbench.md)
