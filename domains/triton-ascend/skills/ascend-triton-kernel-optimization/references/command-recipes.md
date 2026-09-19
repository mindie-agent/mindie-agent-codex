# Report from existing evidence

For paired callable measurements, use the `run` entry in [the Skill](../SKILL.md)
and the callable-input contract (`references/callable-runner.md` in the `ascend-operator-debug` skill).
The command below only aggregates supplied measurements and is optional when
existing evidence is sufficient.

Run with the MindIE plugin's configured Python; the script resolves by its absolute path under the installed Skill directory, and the business checkout stays the working directory:

```text
python /absolute/domain/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py --config optimization.json --results round-results.json
```

The config contains op_name, kernel and its validation evidence, target, cases, baseline measurements and objective. Round results carry candidate measurements and validation. The report applies its numeric KEEP/DISCARD rules to those inputs and checks their lineage and case coverage. These report labels do not prove candidate execution or replace the Agent's assessment of noise and retained changes.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
