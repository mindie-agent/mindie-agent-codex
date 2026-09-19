---
name: vllm-ascend-serving
description: "Start, inspect or stop one managed single-node vLLM Ascend service. Prefill/decode disaggregation uses pd-serving."
---

# vllm-ascend-serving

Start, inspect or stop one managed single-node vLLM Ascend service.

Reuse the native task context and actual business source bindings. Choose model, parallelism and serving options from the request. Resource state and HTTP/models/first-token readiness are separate observations.

## Agent entry

For managed execution, use the [MindIE entry](../mindie-agent/SKILL.md) to activate this task once and pass its credentials to the CLI. An existing active lease is reused; an expired or paused lease needs another explicit invocation. Local evidence-only reports do not start the service.

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/vllm-ascend-serving/scripts/serving.py start --model /models/example --tp 1
```

Use serving.py status or serving.py stop with --execution-id or --service. A service reference is resolved by coordinator within the current task. Pending states retain their execution reference. Restart or release follows the requested lifecycle; no separate allocation, parity command or status ledger is needed.

Start follows preparation and HTTP/models/first-token readiness within
--health-timeout. Use --no-wait when an immediate execution receipt is wanted.
After a bounded wait, continue with the same reference. The CLI checks the `MINDIE_SESSION_ID` and `MINDIE_ACTIVATION`
exported by the activation step against the local session gate before any
remote call. Missing configuration or activation fails closed.

Stdout is a compact receipt with business facts under `result` and a local
`record_ref` for the full envelope. `MINDIE_FULL_RECEIPT=1` requests the full
envelope directly. Healthy startup probes health, models and a completion in
one remote call; logs are fetched only when diagnosing a failed startup.

Use pd-serving for prefill/decode topology, benchmark for measurement, and profiling-collection for profiler-window control.

Use `--host` before `--` to select a coordinator placement host. When the user
authorizes sharing an occupied card, pass one explicit `--devices` card and
`--allow-external-busy` with TP1/DP1. Coordinator still owns the execution and
lease. Relaunch preserves these settings; `--no-allow-external-busy` restores
exclusive admission.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
