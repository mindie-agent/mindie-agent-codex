# Business input example

This is an illustrative configuration shape. Select cases and values for the
actual task. The tool generates report metadata internally; observed output
files are produced by the relevant execution or measurement harness.

Each role parses its vLLM arguments when its managed process starts. The
topology does not run a separate parse-only import before launch. Coordinator
still owns group preparation, resource admission and execution cleanup.

`start` needs only `services` and any optional topology/environment settings.
Connector configuration is passed to vLLM through each service's `args` and
`env`; fill in the options supported by the selected vLLM/connector version.

`status --config` needs only `proxy`; `smoke --config` needs `proxy` and `smoke`.
These operations address an existing proxy and do not start it. The combined
example below may be reused across operations; unused sections are optional.

```json
{
  "group_id": "pd-group",
  "services": [
    {
      "name": "decode",
      "role": "decode",
      "model": "/models/example",
      "tp": 1,
      "args": [
        "--kv-transfer-config",
        "{\"kv_role\":\"kv_consumer\"}"
      ]
    },
    {
      "name": "prefill",
      "role": "prefill",
      "model": "/models/example",
      "tp": 1,
      "args": [
        "--kv-transfer-config",
        "{\"kv_role\":\"kv_producer\"}"
      ]
    }
  ],
  "startup_order": [
    "decode",
    "prefill"
  ],
  "proxy": {
    "base_url": "http://proxy:9000",
    "health_path": "/health"
  },
  "smoke": {
    "path": "/v1/chat/completions",
    "request": {
      "model": "example",
      "messages": [
        {
          "role": "user",
          "content": "hello"
        }
      ]
    }
  }
}
```
