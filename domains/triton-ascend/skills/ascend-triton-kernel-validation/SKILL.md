---
name: ascend-triton-kernel-validation
description: Run an explicit Ascend Triton wrapper against a reference over real shape, dtype, layout and stride cases, or assess existing validation evidence. Use after migration or implementation changes, before performance claims, or for shape-dependent failures in a Triton candidate. Do not use to generate or optimize the kernel, diagnose a non-Triton operator, or localize a whole-model graph failure.
---

# ascend-triton-kernel-validation

Validate one Ascend Triton kernel against its reference over the cases needed by its consumers.

Select shapes, dtype, layout, strides, scalar options and execution modes from the operator contract and affected callers. Include boundary and non-contiguous cases where semantics require them. Numerical agreement must come from the launched candidate kernel.

## Agent entry

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/domain/skills/ascend-triton-kernel-validation/scripts/triton_validation.py run --kernel kernel.py:run --reference reference.py:run --cases cases.py:cases
```

The small runner fixes the three Python files into one owned script and invokes
their named callables. The case factory constructs actual tensor shapes, strides
and scalar arguments; the wrapper supplies the Triton launch and any requested
compile/graph semantics. It does not infer a kernel signature or adapt ModelNew.
See `references/callable-runner.md` in the `ascend-operator-debug` skill for callable inputs and execution scope.

Existing runner/profiler evidence can be assessed directly. Optional report mode
keeps `--config validation.json --kernel kernel.py --results case-results.json`.
That report combines supplied results; source lint is advisory and understands
only the ModelNew.forward convention.

The report separates `numerical_status` from `candidate_execution`. Case status
and source lint alone do not prove actual candidate NPU execution, so the tool
retains that fact as unknown and an otherwise passing report is inconclusive.
Assess existing runner or profiler evidence for the actual launch; no new
attestation form or repeat run is required when valid evidence already exists.

A failing candidate returns to ascend-triton-operator-development. A fully passing matrix can proceed to ascend-triton-kernel-optimization.

Read the relevant detail only when needed:

- [case design](references/case-design.md)

- [Existing-evidence report command](references/command-recipes.md)
- [Existing-evidence report input example](references/inputs.md)
