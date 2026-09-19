---
name: ascend-triton-workflow
description: Consolidate existing Ascend Triton development, correctness and optimization evidence when asked for a report or a summary across stages. Operator development, validation and tuning do not require an aggregate report. Requires prior manual MindIE Agent activation in this task; load on demand, never preemptively.
---

# Ascend Triton evidence report

Summarize supplied stage manifests within the requested report scope.
Development evidence identifies the implementation, correctness evidence covers
tested cases, and optimization evidence records measured tuning. The report
tool links existing observations and does not launch these stages.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
python3 skills/ascend-triton-workflow/scripts/triton_workflow.py --config operator.json --development development/manifest.json --validation validation/manifest.json
```

The config describes the operator and the stages this report should cover.
The report checks artifact availability, case scope and kernel identity and
records missing or unrelated evidence. `required_stages` is a report input,
not a prerequisite for ordinary operator work. Linking and report identifiers
are generated internally.

Imported numerical results and latency measurements do not by themselves prove
candidate NPU execution; this remains
unknown in the aggregate status. Assess those limits alongside the actual runner
or profiler evidence. An inconclusive report does not by itself require another
experiment or determine whether the user's development task is complete.

See the [report input example](references/inputs.md) for configuration and
[command options](references/command-recipes.md) for optional stage inputs.
