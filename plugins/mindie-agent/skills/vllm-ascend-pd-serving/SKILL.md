---
name: vllm-ascend-pd-serving
description: "Start, inspect, smoke-test or stop an Ascend prefill/decode deployment with NIXL, Mooncake or another KV connector as one managed topology."
---

# vllm-ascend-pd-serving

Start and inspect a prefill/decode deployment as one coordinator-owned topology.

Choose prefill/decode roles, parallelism, connector options and proxy routing from the deployment requirement. A successful HTTP response proves request handling; KV transfer needs connector-specific evidence.

## Agent entry

For managed execution, use the [MindIE entry](../mindie-agent/SKILL.md) to activate this task once and pass its credentials to the CLI. An existing active lease is reused; an expired or paused lease needs another explicit invocation. Local evidence-only reports do not start the service.

Run this Skill's script with the Python interpreter configured for the MindIE plugin (the `python` value in the `MINDIE_AGENT_CONFIG` JSON). Resolve the script by its absolute path under the installed Skill directory; the business checkout stays the working directory. No old checkout adapter, project environment or bootstrap is loaded.

```text
python /absolute/plugin/skills/vllm-ascend-pd-serving/scripts/pd_serving.py start --config topology.json
```

The start config contains services; group_id and startup_order are optional. Set each role's connector options through its vLLM `args` and `env`. status and stop accept --service or --execution-id without a local lifecycle file. status may take --config containing proxy health settings; smoke takes --config containing proxy and smoke workload. The proxy must already exist. Coordinator owns resource state and teardown.

Use ordinary serving for a colocated service. Route rank/connector hangs to distributed-debug.

Read the relevant detail only when needed:

- [Business input example](references/inputs.md)
