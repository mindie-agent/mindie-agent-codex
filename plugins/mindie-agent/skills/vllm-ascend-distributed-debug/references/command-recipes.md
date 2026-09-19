# Agent call

Run with the MindIE plugin's configured Python; the script resolves by its absolute path under the installed Skill directory, and the business checkout stays the working directory:

```text
python /absolute/plugin/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py --config topology.json --events rank-events.jsonl
```

The config supplies expected_world_size, ranks and optional groups/endpoints. Event files supply observed facts in capture order. The report checks mappings and structured collective observations; absent events remain capture gaps rather than proof that a rank skipped the collective. No case initialization or event-registration steps are required.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
