# Optional report inputs

Run with the MindIE plugin's configured Python; the script resolves by its absolute path under the installed Skill directory, and the business checkout stays the working directory:

```text
python /absolute/domain/skills/ascend-triton-workflow/scripts/triton_workflow.py --config operator.json --development development/manifest.json --validation validation/manifest.json --optimization optimization/manifest.json
```

Supply the manifests relevant to the configured report scope using
`--development`, `--validation` and `--optimization`. The config's
`required_stages` determines which missing inputs are reported; an optimization
report scope also includes validation so measurements can be related to
correctness evidence. Reuse that existing evidence. The tool checks scope,
available artifacts, passing cases and kernel identity without launching stages.

Use `--help` for argument details. Reports create their own identifiers and
output directories; `--output-dir` selects a destination when needed. Existing
observed inputs are sufficient; no parent task association is required.
