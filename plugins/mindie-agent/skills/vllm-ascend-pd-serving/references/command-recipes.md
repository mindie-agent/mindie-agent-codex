# Agent call

From the repository root:

```text
python3 skills/vllm-ascend-pd-serving/scripts/pd_serving.py start --config topology.json
```

The start config contains services; group_id and startup_order are optional. Set each role's connector options through its vLLM `args` and `env`. status and stop accept --service or --execution-id without a local lifecycle file. status may take --config containing proxy health settings; smoke takes --config containing proxy and smoke workload. The proxy must already exist. Coordinator owns resource state and teardown.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
