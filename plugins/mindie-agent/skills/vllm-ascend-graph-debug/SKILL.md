---
name: vllm-ascend-graph-debug
description: "Diagnose Ascend graph compile, capture or replay failures and eager-versus-graph divergence. Reduced operator faults use operator-debug."
---

# vllm-ascend-graph-debug

Locate the first divergence between eager and graph executions or investigate graph compile, capture and replay failures.

Fix inputs and compare corresponding stages, ranks and steps. Separate compile, capture and replay; instrumentation that synchronizes the device can change the failure. Use bounded tensor capture only when existing outputs cannot distinguish the hypothesis.

## Agent entry

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py --eager eager.jsonl --graph graph.jsonl
```

Snapshot identity is read from sidecars, or --eager-identity and --graph-identity. The report compares observed identities and samples with finite tolerances, emits the first divergence and a comparability certificate, and retains missing identity as inconclusive. Its conclusion applies to the supplied snapshots.

Use correctness-validation to establish the reproduction, tensor-dump to capture intermediate stages, and operator-debug after reducing to one call.
