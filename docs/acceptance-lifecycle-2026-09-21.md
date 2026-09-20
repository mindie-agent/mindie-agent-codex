# Lifecycle acceptance checkpoint

2026-09-21. This is a pre-release evidence record. The inherited [nine principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md) guide design and review; root owns final acceptance. Developer checks and native results are separate.

## Actual macOS results

All Codex model cases used gpt-5.6-luna / max, with bounded execution and zero automatic retries. Isolated native profiles preserved the user's production configuration and Hook trust.

| Case | Actual evidence | Limits |
| --- | --- | --- |
| Read-only first use | Native task `01a0bf5c-3d32-7690-be92-4f68581b235f`, 93.22s: first choice persisted, real query/read, invalid ref rejected before execution and a later valid query succeeded; admission failures zero | Earlier candidate; no contribution |
| Same-task update | Native task `01a0bf81-c433-7521-9cb7-49640e9c4b0c`, candidate5fa43d8→3aefd89: actual GitHub pin builds, native install/version readback; same authorization, old entrypoint retrieved2hits; original remote job running→stop→cancelled with descendants drained | Explicit candidate switch, not OS timer/main polling; Hook disabled. Initial remote request was not_sent because the owned container had expired; an explicit follow-up occurred only after verifying its restart |
| Final installed entry | Candidate `785bdcb08538ef540d5e1946defc83cfd72c933f`, native task `01a0bfd1-ad02-7773-9492-8901e85940e9`, 102.99s: actual installed plugin, no inherited MINDIE_AGENT_CONFIG and no manual MCP override; ordinary Skill bridge init/read-only/activate succeeded, native mindie-knowledge query returned2hits and explain returned3394characters | Sharing off; admission enabled1/failures0, zero captures and collection turn databases; service stopped after test. Hook deliberately disabled, so changed Stop trust is not established |

The final native task explained the practical reuse: round inputs to the tested FP16/BF16 dtype before computing a FP32 CPU reference, retaining the observed software/shape/single-device limits. It did not claim a new NPU execution, serving result or performance measurement.

Packaging binds the same installation configuration into MCP, Hook, ordinary Skill entry and helper dispatch. Initial source/dev fallback remains available; installed configuration takes precedence over ambient developer configuration. An explicit CLI path is propagated consistently into its helper. Changed binding/Stop dependencies require fresh native Hook review; the updater never edits the trust store.

109 developer checks and remote adapter CI passed for785bdcb. These checks cover the reviewed failure paths but do not replace native acceptance.

## Pending

- Changed native Stop trust and the final contribution capture/organizer/PR/exact-receipt cleanup/Bot/new-consumer loop.
- Actual scheduled main resolution and native failure-after-install rollback evidence.
- Optional feedback/withdrawal and real Windows host acceptance.

Production remains on the previous installed candidate. The lifecycle PR remains a draft. Old business Skills and profiling are deferred. Earlier evidence proves its recorded code and scope; it is not automatically a release verdict for every later candidate.
