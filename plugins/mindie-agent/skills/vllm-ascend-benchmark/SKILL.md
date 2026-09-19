---
name: vllm-ascend-benchmark
description: "Measure throughput and latency of one vLLM service, including workload sweeps. Use performance-regression for controlled baseline-versus-candidate comparisons."
---

# vllm-ascend-benchmark

Measure a vLLM service with one or several benchmark iterations and return raw and normalized metrics.

Choose input/output lengths, concurrency, request rate and endpoint for the intended workload. User choices override presets and nightly examples. Report variance and failures alongside throughput and latency.

## Agent entry

For managed execution, use the [MindIE entry](../mindie-agent/SKILL.md) to activate this task once and pass its credentials to the CLI. An existing active lease is reused; an expired or paused lease needs another explicit invocation. Local evidence-only reports do not start the service.

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/vllm-ascend-benchmark/scripts/bench_run.py --model /models/example --runs 3 --warmup-runs 1
```

Use --execution-id to measure an existing service, or let the workflow start and clean up its own service. --serve-args and --bench-args forward business options; --preset supplies reusable defaults. The managed interpreter and actual launch observations are recorded with measurements.

Use performance-regression for code comparisons: it binds actual local worktrees and handles alternating runs. Use correctness-validation for accuracy claims.
