# Agent call

Run with the MindIE plugin's configured Python; the script resolves by its absolute path under the installed Skill directory, and the business checkout stays the working directory:

```text
python /absolute/plugin/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py --eager eager.jsonl --graph graph.jsonl
```

Snapshot identity is read from sidecars, or --eager-identity and --graph-identity. The report compares observed identities and samples with finite tolerances, emits the first divergence and a comparability certificate, and retains missing identity as inconclusive. Its conclusion applies to the supplied snapshots.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
