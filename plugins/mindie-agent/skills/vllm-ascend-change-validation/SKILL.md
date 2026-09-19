---
name: vllm-ascend-change-validation
description: Produce a vLLM or vllm-ascend validation report from an accessible diff and existing experiment evidence when asked to consolidate results or document their coverage. Ordinary code review, test selection and experimental validation do not require this report. Requires prior manual MindIE Agent activation in this task; load on demand, never preemptively.
---

# Change validation evidence report

Summarize how supplied validation evidence relates to a code change. The diff
defines the changed behavior; run manifests identify observed revisions,
outcomes and artifacts.

Build, numerical, graph, distributed and performance evidence cover different
failure modes. Describe coverage using the observed code states and actual test
scope, retaining uncertainties that matter to the requested conclusion.
Producing a report requires no runtime, task allocation or new experiment.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
python3 skills/vllm-ascend-change-validation/scripts/change_validation.py --baseline BASE --candidate HEAD --repo-root source --evidence correctness/manifest.json performance/manifest.json
```

Use `--diff-file` for an already captured diff. The report summarizes changed
files and supplied manifests, checks artifact availability and reuses existing
comparability evidence to identify observed source revisions. It creates no test
plan from path keywords and no fallback NPU smoke requirement for unclassified
changes.

Individual run outcomes and source matches remain visible. The aggregate stays
inconclusive about complete change validation: the Agent decides whether the
evidence covers the actual changed behavior. Missing or unrelated evidence is
reported as a limitation, without discarding usable artifacts or requiring a
new parent task association.

An uncovered behavior is a report limitation. Whether it needs another check
depends on the user's actual objective, not the aggregate status alone.
See [report options](references/command-recipes.md) for captured diffs and outputs.
