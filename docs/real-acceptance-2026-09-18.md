# Real-source and remote NPU acceptance — 2026-09-18

The single-domain loop passed with actual Codex tasks, native hooks, MCP calls,
remote NPU execution and independent model judgments. Grok's event subscription
is configured, but event delivery has **not** passed acceptance.

## Deployment and versions

Codex, the knowledge service, capture, organizer and judge ran locally on macOS.
Only device execution ran in the remote Ascend container, through the plugin's
eleven selected upstream remote-dev tools. No Codex credentials or knowledge
service were installed remotely.

- Codex CLI: 0.153.4; local runtime: Python 3.11.15.
- Knowledge runtime: `7cf499f43d996710b29283333a85fceaf99e8889`.
- remote-dev: `a65362882a85b4d460be3e1d15e90de9fb507e70`.
- Installed plugin: `0.1.0+codex.20260918045451`.
- Remote image: `sha256:a68d070238dc52c6b9f09401346022acc47a06c017e9e5a5ced3c45cd4371b31`.
- Runtime: Python 3.12.13, torch 2.10.0+cpu, torch_npu 2.10.0.post2;
  actual device reported by torch_npu: `Ascend910_9362`.

The dedicated acceptance container mapped one unused physical NPU plus manager
devices. Its driver mount was read-only. Existing containers were not modified.
The acceptance container was stopped afterward, retaining scripts/results in its
host bind directory. Temporary local replicas were shut down; the normal local
knowledge service remains available.

## 1. Organization feed and actual source changes

The VA knowledge maintainer was operated in the user's Grok Bot app. Its permanent
instructions and daily, weekly and event routines now publish to the organization
repository's data branch:

[mindie-agent/knowledge:knowledge/vllm-ascend](https://github.com/mindie-agent/knowledge/tree/knowledge/vllm-ascend).

The branch is independent of the code branch. The personal fork is no longer the
formal publication destination. The maintainer processed an actual head change
in [vLLM-Ascend PR 16656](https://github.com/vllm-project/vllm-ascend/pull/16656),
revising source conditions and advisory material at head
`138163458f134fba039ab49e28ebe75e9116c32e`. This was a requested incremental run,
not proof of a webhook-triggered run.

The normal local service fetched and verified the published generation:

- Publisher commit: `ba8a9e7ce07b115f2145b22114be66715613e50c`.
- Generation: `cb9fa0ba015a41cd97b1f630a49f7de6`.
- Manifest SHA256: `39de37b30d42ecfee42b6c8fcb54474192c008ae7df1daaf1b903114ce0e03c9`.
- 16 topics and 16 cases became searchable; seven maintenance diaries were excluded.
- A topic lacking applicability was held out by the publisher.
- Unchanged polling downloaded zero document bodies. Feed failure retains the
  last valid generation; updates keep historical references explainable.

Two independent local source-investigation tasks also consumed the actual PR
material and recorded use. Their source-control-flow checks are separate from the
NPU validation below; they do not prove PR 16656's numerical correctness.

## 2. Actual remote NPU execution

Task A investigated device visibility and exercised `torch_npu.npu_rms_norm`
against CPU float32 references. Its ordinary final response reached the real Stop
hook and a fresh organizer. Two sanitized experiences were explicitly reviewed and
authorized for distribution: container logical numbering, and bounded RMSNorm
validation. Automatic publication remains an opt-in setting; this run did not
exercise unattended public upload of experience.

Task B used a separate local replica and a new native session. It queried and
explained both experiences through MCP, then wrote and executed its own NPU script
on new shapes and inputs through `mindie-remote-dev`.

| Task | Shapes | dtypes | Seeds | Actual passing cases |
|---|---|---|---|---:|
| A | 4×1024, 7×4096 | FP16, BF16 | 1729, 2718 | 8 |
| B | 1×3072, 3×2048 | FP16, BF16 | 841, 991 | 8 |

Both asserted NPU devices, synchronized execution and checked changed input,
weight and output hashes. Elementwise bounds were `abs(error) <= atol + rtol *
abs(reference)`, with atol=rtol=0.002 for FP16 and 0.016 for BF16. These are explicit
bounds for this isolated test, not universal tolerance recommendations. A also
checked rstd against its reference. B's worst absolute errors were 0.00181246 for
FP16 and 0.01461315 for BF16. Both execution jobs exited zero.

Task B's NPU allocation plus its conservative explicit CPU tensor budget was
19.17 MB, below the 100 MB test bound; this excludes driver/context overhead.
Its supervised execution took approximately 11 seconds within a 120-second
deadline. Remote and local wall clocks differed, so cross-machine timestamps were
not used as latency measurements.

A concrete reusable finding was independently reproduced: in this single-device
container, physical device 8 mapped to logical `npu:0`. Visibility `8` reported
zero devices, while command-scoped visibility `0` reported one and ran the NPU
operator. No global configuration or package change was needed. `npu-smi` was
absent inside the container; the actual tensor execution supplied device evidence.

Scripts and JSON results were pulled with `remote_artifact_pull` and hash-verified.
Task B result SHA256:
`d79bdcbaf379d8f30e35b22037a364491d4e162026d74bf106693f717a131252`.
Task B script SHA256:
`7b70e40953a8ae63d3f5e6da2540a43f01f236698f8ff6b19f6cc2af6742f98e`.

## 3. Independent feedback and retrieval

B called `knowledge_use` after actual application; it did not cast votes. Its
normal Stop outcome was synchronized to the local authority. Fresh judge calls
classified both uses as helpful. Feedback returned to B and a fresh replica C.
The organizer, consumer and judge were separate actual model executions.

For the same six-entry published corpus and query
`Ascend container logical NPU numbering RMSNorm`:

| Experience | Before B's feedback | After feedback reached C |
|---|---:|---:|
| Container logical numbering | 9.60328670 | 13.62771685 |
| Bounded RMSNorm validation | 2.84465770 | 4.03676271 |
| Each usefulness multiplier | 1.0 | 1.419068 |

Each experience had exactly one independent helpful use. No maintenance or judge
errors remained. These scores measure retrieval usefulness, not factual confidence
or general task success rates.

## 4. Grok monitoring boundary

The enabled `PR 事件增量维护` routine (`pr-2`) lists PR opened/pushed/closed/merged
events for `vllm-project/vllm-ascend` and `vllm-project/vllm`. Existing daily and
weekly routines are retained. All three target the organization data branch and
share an exclusive publish lock.

At the 13:06 Asia/Shanghai check, native automation status still reported
**never run**: no event ID, delivery receipt or event-triggered publication existed.
Subscription configuration and a successful manual incremental run therefore do
not establish real-time monitoring. Further read-only diagnosis confirmed that
upstream PRs were opened after subscription, including
[PR 16857](https://github.com/vllm-project/vllm-ascend/pull/16857) at 04:42 UTC.
The bot had no delivery diagnostic surface, and its CLI credential could not read
GitHub App installations (403). Successful public repository reads do not establish
the routine connection's repository event access. The precise delivery failure
remains unresolved; repository App/webhook scope is a possibility, not a confirmed
cause. Daily incremental maintenance remains the fallback. No dummy upstream
changes or permission expansion were used to create a test event.

## 5. Checks and evidence boundary

Latest knowledge runtime CI passed all three workflows:

- [Package checks](https://github.com/mindie-agent/knowledge/actions/runs/35308985977).
- [Native desktop contracts](https://github.com/mindie-agent/knowledge/actions/runs/35308985990).
- [Independent knowledge intake](https://github.com/mindie-agent/knowledge/actions/runs/35308985988).

The platform runs include macOS and Windows. A Windows path-key failure was fixed
with POSIX archive paths. Persistent MCP reconnection after an owned service restart
has a real-process regression check. Adapter unit tests, plugin validation and
Skill validation passed; the actual stdio wrapper exposed eleven remote-dev tools
and rejected a tool outside that set.

Private raw task events, hook outcomes, service state and remote job receipts stay
under ignored `.local/acceptance/real-20260918` and
`.local/acceptance/remote-20260918`. This public report contains sanitized results.
The earlier [synthetic acceptance](acceptance-2026-09-18.md) remains a bounded
repeatable control-plane check.

This acceptance establishes the tested one-domain mechanism and isolated NPU
operator cases. Whole-model validation, general productivity gains, unattended
public experience publication, production-scale retrieval, Desktop cross-domain
task control and other Harnesses are not established by this run.
