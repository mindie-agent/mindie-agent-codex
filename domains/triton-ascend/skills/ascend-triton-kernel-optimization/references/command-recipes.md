# Report from existing evidence

For paired callable measurements, use the `run` entry in [the Skill](../SKILL.md)
and [callable inputs](../../ascend-operator-debug/references/callable-runner.md).
The command below only aggregates supplied measurements and is optional when
existing evidence is sufficient.

From the repository root:

```text
python3 skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py --config optimization.json --results round-results.json
```

The config contains op_name, kernel and its validation evidence, target, cases, baseline measurements and objective. Round results carry candidate measurements and validation. The report applies its numeric KEEP/DISCARD rules to those inputs and checks their lineage and case coverage. These report labels do not prove candidate execution or replace the Agent's assessment of noise and retained changes.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
