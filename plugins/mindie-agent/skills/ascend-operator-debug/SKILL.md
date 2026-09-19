---
name: ascend-operator-debug
description: Reduce an Ascend failure to one operator and run an explicit candidate callable against a reference over real input cases. Use for operator crashes, unsupported dtype or layout errors, shape-dependent numerical mismatches, or workspace API faults. Do not use for whole-model graph localization, multi-rank failures, performance benchmarking, or profiler analysis. Requires prior manual MindIE Agent activation in this task; load on demand, never preemptively.
---

# ascend-operator-debug

Reduce a reproduced failure to one operator and compare its actual outputs against a trusted reference.

Keep dtype, shape, physical layout, strides and eager/compile/graph mode explicit in the business cases. Prefer the smallest input that still reproduces the failure. A passing isolated call supports that call only; a model-level fix needs a model rerun.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
python3 skills/ascend-operator-debug/scripts/operator_debug.py run --kernel kernel.py:run --reference reference.py:run --cases cases.py:cases
```

The three Python files are fixed into one managed script. `cases(device)` yields
actual arguments; the runner invokes the candidate and reference, compares their
outputs, preserves failures, and returns an execution reference with a compact
result. NPU is the default; no case-registration or report step is needed.
See [callable inputs and scope](references/callable-runner.md) for one complete
example, tolerance options and supported input copying.

Reuse existing evidence when it answers the question. The earlier
`--config operator.json --results case-results.json` entry remains an optional
report over supplied observations; it does not launch or certify a candidate.

Use ascend-tensor-dump while the first divergent stage is unknown. Use the Triton skills for a Triton candidate.

Read the relevant detail only when needed:


- [Existing-evidence report command](references/command-recipes.md)
- [Existing-evidence report input example](references/inputs.md)
