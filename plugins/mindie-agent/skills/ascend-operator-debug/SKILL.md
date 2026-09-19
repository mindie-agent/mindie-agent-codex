---
name: ascend-operator-debug
description: "Reproduce one Ascend operator failure and compare a candidate callable with a reference. Use for reduced operator crashes, dtype/layout errors or numerical mismatches."
---

# ascend-operator-debug

Reduce a reproduced failure to one operator and compare its actual outputs against a trusted reference.

Keep dtype, shape, physical layout, strides and eager/compile/graph mode explicit in the business cases. Prefer the smallest input that still reproduces the failure. A passing isolated call supports that call only; a model-level fix needs a model rerun.

## Agent entry

For managed execution, use the [MindIE entry](../mindie-agent/SKILL.md) to activate this task once and pass its credentials to the CLI. An existing active lease is reused; an expired or paused lease needs another explicit invocation. Local evidence-only reports do not start the service.

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/ascend-operator-debug/scripts/operator_debug.py run --kernel kernel.py:run --reference reference.py:run --cases cases.py:cases
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
