# Report from existing evidence

For actual candidate execution, use the `run` entry in [the Skill](../SKILL.md)
and the callable-input contract (`references/callable-runner.md` in the `ascend-operator-debug` skill).
The command below only summarizes supplied observations and is optional when
existing evidence is sufficient.

Run with the MindIE plugin's configured Python; the script resolves by its absolute path under the installed Skill directory, and the business checkout stays the working directory:

```text
python /absolute/domain/skills/ascend-triton-kernel-validation/scripts/triton_validation.py --config validation.json --kernel kernel.py --results case-results.json
```

The config contains op_name, reference, target, cases and tolerances. The report combines supplied numerical results and records their coverage. `lint_triton_source.py` provides limited syntactic observations about ModelNew.forward wrappers; it cannot prove a launch or absence of computation fallback. Candidate execution remains unknown unless assessed from actual runner or profiler evidence.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
