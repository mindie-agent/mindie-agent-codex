# General remote-dev boundary and actual macOS acceptance

Candidate adapter: `27edad31975b1f5b0f3d785bfd666157177cf5d4`.
Native installed version: `0.1.0+codex.20260920081831793374`.
Codex native discovery confirmed this version and the unchanged Stop command as trusted.

## Behavior

Remote MCP is available on demand in every native task. No MindIE lease, knowledge service, capture or organizer is required. Caller identity comes from native per-call task and turn metadata. Job records live under a separate task directory; request receipts use SQLite and do not evict consumed identities or impose a lifetime call ceiling. Three consecutive transport/runtime failures pause that task's remote calls; explicit native recovery releases the pause without replaying calls. Each call retains the 65-second process deadline, cancellation cleanup, bounded output and zero automatic retry.

Knowledge MCP still requires explicit MindIE activation. Stop capture additionally requires sharing enabled and the authorized project scope. Installing/discovering remote tools does not grant capture permission.

Removed the execution wrappers and coordinator installation dependency whose bundled domain Skills had already been retired. The deferred profiling Skill was not modified.

## Real native task, not a component substitute

A fresh `gpt-5.6-luna / max` task used the actually installed plugin without reading the MindIE Skill or activating it. It completed in 230.06 seconds with six remote MCP calls. It used a dedicated isolated container mapping one previously idle physical NPU, corrected only its command's device environment, ran the prepared RMSNorm check, and read back the actual JSON artifact.

- Physical device node 8 was present; `ASCEND_RT_VISIBLE_DEVICES=8` gave `available=False`, `device_count=0`.
- `ASCEND_RT_VISIBLE_DEVICES=0` gave one visible device; device name `Ascend910_9362`.
- FP16: finite, allclose, max absolute error `0.0019469261169433594`, atol/rtol `0.002`.
- BF16: finite, allclose, max absolute error `0.015557289123535156`, atol/rtol `0.016`.
- Shape `[5,1536]`, seed `20260920`, epsilon `1e-6`, explicit tensor bound `675840` bytes per case; CPU float32 reference uses the same dtype-rounded inputs.
- The job exited 0. Independently pulled artifact SHA-256 agrees with the native result: `c0fd57c6a30f2e85666323b47a014c6c211fce506a0e2f083e03e8a24dab897a`.
- No lease was created for this task; total existing lease rows remained 5. Sharing stayed disabled/unconfigured. No capture or organizer was launched.
- Measured usage: input `253444`, including cached input `211968`; output `10469`, including reasoning `7368`. This is one correctness/entry-boundary case, not evidence of a token saving.

This proves general remote invocation and one bounded real NPU computation on macOS. It does not establish serving performance, multi-card behavior, knowledge usefulness, community publication, or Windows desktop acceptance. Source/result review and the full knowledge loop have separate receipts; a failed capture is not counted as a pass.

Component checks separately cover missing/contradictory identity, task-local jobs, corrupt receipt rejection, repeated-request rejection across bridge instances, normal request-number reuse in another turn, failure pause/recovery and more than 4096 lifetime calls. A follow-up change removes internal job authorization from model-facing results; its focused regression checks pass and it requires final packaging readback.
