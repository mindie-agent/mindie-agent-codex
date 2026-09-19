---
name: ascend-triton-kernel-optimization
description: Optimize a correctness-passed Ascend Triton kernel using paired callable measurements, profiler evidence and hardware reasoning. Use for single-kernel latency or throughput improvement after the required correctness cases pass. Do not use to create the first correct kernel, bypass failed validation, assess whole-model serving regressions, attribute model HBM, or diagnose a non-Triton operator. Requires prior manual MindIE Agent activation in this task; load on demand, never preemptively.
---

# ascend-triton-kernel-optimization

Optimize an already validated Ascend Triton kernel using measured bottlenecks and repeatable latency evidence.

Choose one bottleneck hypothesis per round. Consider UB live set, physical cores and MTE/Vector/Scalar overlap. Compare repeated per-shape measurements with a reference baseline; retain a change only when the gain exceeds noise and correctness still covers the candidate.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
python3 skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py run --kernel kernel.py:run --reference reference.py:run --cases cases.py:cases --warmups 3 --repeats 20
```

This performs one paired measurement of the supplied wrappers in one owned
execution, checking outputs before timing and on every measured pair. It warms
each wrapper, alternates pair order, and retains individual synchronized wall
times separately from first-call compilation. Copying inputs is outside the
timed region. Results describe callable latency, not isolated device kernel time.
Noise assessment, the next edit and KEEP/DISCARD remain with the Agent.
See [callable inputs and scope](../ascend-operator-debug/references/callable-runner.md).

Reuse existing measurements when sufficient. Optional
`--config optimization.json --results round-results.json` aggregates earlier
observations without launching a new candidate or proving its execution.

Use ascend-triton-kernel-validation when correctness is incomplete. Use profiling-analysis for whole-model performance attribution.

The report aggregates supplied evidence. Imported numerical results and latency
measurements do not by themselves prove candidate NPU execution; this remains
unknown in the aggregate status. Keep the measured outcomes and use existing
runner or profiler evidence in the task assessment. That report limitation adds
no prerequisite for work already supported by valid execution evidence.

Read the relevant detail only when needed:

- [profiling decision tree](references/profiling-decision-tree.md)
- [ascend techniques](references/ascend-techniques.md)
- [Existing-evidence report command](references/command-recipes.md)
