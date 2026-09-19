# Report options

From the repository root:

```text
python3 skills/vllm-ascend-change-validation/scripts/change_validation.py --baseline BASE --candidate HEAD --repo-root source --evidence correctness/manifest.json performance/manifest.json
```

Use `--diff-file` for an existing diff. The report preserves supplied run outcomes,
artifact availability and observed source matches. The Agent assesses whether
that evidence covers the changed behavior; no automatic required test plan is generated.

Use `--help` for argument details. Reports create their own identifiers and
output directories; `--output-dir` selects a destination when needed. Summarize
the supplied evidence without adding task records or new experiments solely to
change the aggregate report status.
