---
name: vllm-ascend-distributed-debug
description: "Diagnose vLLM Ascend failures that depend on rank count, nodes, process groups, collectives or distributed endpoints using topology and per-rank evidence."
---

# vllm-ascend-distributed-debug

Diagnose failures whose behavior depends on ranks, nodes, process groups, collectives or distributed endpoints.

Start from the failing topology and per-rank timeline. Distinguish missing rank startup, rendezvous, collective ordering and asymmetric workloads. Reduce topology only when the reduced case still reproduces the signature.

## Agent entry

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py --config topology.json --events rank-events.jsonl
```

The config supplies expected_world_size, ranks and optional groups/endpoints. Event files supply observed facts. The report validates mappings and event order and generates its evidence automatically; no case initialization or event-registration steps are required.

Use graph-debug when eager passes and graph fails independent of topology. Performance imbalance with a successful run belongs to profiling-analysis.

Read the relevant detail only when needed:

- [Business input example](references/inputs.md)
