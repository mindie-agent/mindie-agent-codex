---
name: ascend-memory-profiling
description: "Attribute Ascend serving HBM to weights, KV cache, HCCL, activations and runtime from captured evidence. Use for component memory attribution; quick usage checks use the fleet monitor."
---

# ascend-memory-profiling

Attribute serving HBM to fixed overhead, weights, KV cache, HCCL, activations and runtime with explicit evidence.

Prefer measured msprof and npu-smi evidence, then startup logs and tensor headers. Header byte sizes are exact; per-device sharding and component labels may be inferred. Model-config estimates are a fallback. Keep residual memory visible instead of forcing categories to balance.

## Agent entry

For managed execution, use the [MindIE entry](../mindie-agent/SKILL.md) to activate this task once and pass its credentials to the CLI. An existing active lease is reused; an expired or paused lease needs another explicit invocation. Local evidence-only reports do not start the service.

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/ascend-memory-profiling/scripts/mem_collect.py --help
```

mem_collect.py accepts the serving workload and captures evidence through the managed service. mem_analyze.py consumes the collection output. Keep collection parameters tied to the user question. Standalone collection embeds its local msprof wrapper in one managed serving execution, forwards the requested serving configuration, stops that execution, and exports only its recorded runtime directory. Missing CSV evidence returns an incomplete result. Attach mode reuses an execution and leaves a live service running; resume requires the same execution ID.

A ready-state sample and a post-inference sample do not establish an activation peak. Report missing idle baselines as unknown, keep unassigned process-level msprof data separate from device totals, and label tensor-name sharding as an estimate.

Use profiling analysis for kernel timing, bubbles and communication latency.

Read the relevant detail only when needed:

- [methodology](references/methodology.md)
- [msprof_fields](references/msprof_fields.md)
