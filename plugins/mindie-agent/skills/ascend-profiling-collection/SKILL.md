---
name: ascend-profiling-collection
description: "Collect one torch-profiler workload on a selected Ascend container and return verified per-rank artifacts. Use to capture new profiling data; existing data uses profiling-analysis."
---

# ascend-profiling-collection

Collect one torch-profiler case, bracket a real workload, export per-rank data and return an analysis-ready manifest.

Choose a capture window and workload that expose the suspected bottleneck. Keep token counts and concurrency representative. For multimodal cases pass the local image and target height; encoding is platform-independent.

Collection starts from the supplied workload and records actual results. It does
not query knowledge before startup or on failure. If related experience would
help, the Agent can use the knowledge MCP tools independently; this is optional.

## Agent entry

For managed execution, use the [MindIE entry](../mindie-agent/SKILL.md) to activate this task once and pass its credentials to the CLI. An existing active lease is reused; an expired or paused lease needs another explicit invocation. Local evidence-only reports do not start the service.

Run this Skill's script with the Python interpreter configured for the MindIE plugin. Resolve the script from the installed Skill directory; the business checkout is only the working directory. No old project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py --model /models/example --served-model-name example --tp 1 --tag case --mode enforce_eager --request-kind text --benchmark-output-tokens 128
```

The workflow starts or observes the managed service, controls /start_profile and /stop_profile, runs analyse(), verifies expected rank outputs and records workload success. DB export is the default. Large traces stay near the data; the resulting manifest can be passed directly to analysis.

Stdout is a compact receipt: workload/rank status, owned execution reference and
`manifest_ref`. Per-request responses and detailed rank outputs remain in that
complete local manifest. Pass its path to analysis, or save the receipt itself
and pass that file; analysis resolves the recorded manifest without recollecting.
Read `manifest_ref` when per-request or per-rank details are needed; stdout always stays bounded.

Use profiling-analysis for existing traces, memory-profiling for HBM attribution, and benchmark for throughput measurements without tracing.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
