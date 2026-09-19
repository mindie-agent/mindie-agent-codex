# Agent call

Run with the MindIE plugin's configured Python; the script resolves by its absolute path under the installed Skill directory, and the business checkout stays the working directory:

```text
python /absolute/domain/skills/ascend-triton-operator-development/scripts/triton_development.py --config operator.json --kernel kernel.py --validation-manifest validation/manifest.json
```

The business config contains op_name, mode, source, reference, target, cases and tolerances. The report consumes the actual kernel and validation manifest, checking kernel identity and passing case coverage. Optional --semantic-report and --sketch attach useful design artifacts.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
