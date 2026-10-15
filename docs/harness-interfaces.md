# Codex Harness interface evidence

The machine-checked registry is `plugins/mindie-agent/harness-compatibility.json`.
It declares two singleton intervals: Codex `0.153.4` and
`0.162.0-alpha.17.2`. The declaration does not claim continuous support between
those endpoints. CI resolves each registered production symbol and test method,
then uses the shared metadata validator to check every declared interval against
every used interface.

These are candidate interface declarations, not a released support guarantee or
proof of cancellation for every native agent. The active-main-thread route and
the unresolved child-agent boundary below have separate acceptance evidence.

Official source revisions used for this boundary are
`3d2ee51ca2d5db578f328aa75e20aa22c0197c9a` and
`740e5af33c71225640e0c1c1555c514c2c93ab74`, respectively.

## Native management

Both source revisions declare `hooks/list`, `config/read`, and
`config/value/write` in `codex-rs/app-server-protocol/src/protocol/common.rs`.
The hook and config modules define hook identity/hash/enabled/trust state,
`includeLayers`, the exact user file, and optimistic `expectedVersion`.
The plugin code also uses the quoted `hooks.state.<key>.trusted_hash` field.

On macOS Codex 0.153.4 and native Windows Codex 0.162.0-alpha.17.2, isolated
native app-server experiments exercised old trusted SessionStart/Stop, changed
commands after native upgrade, native CAS of those exact hashes, and actual
new SessionStart/Stop execution. A third explicitly disabled/untrusted hook
remained disabled and never ran. No trust bypass was configured. The responses
came from loopback synthetic SSE; there were no external provider/model calls.
These are native interface tests, not paid capture or publication acceptance.

The product `native_hooks.py` was then exercised on macOS against a real
isolated native installation. It changed only the official marketplace's source
using native config CAS, verified unchanged installed version/enabled state,
unchanged old payload bytes and unchanged hook approvals, installed the selected
Git subdirectory at an exact SHA, and restored only previously approved hooks.
Successful trust writes were verified through `hooks/list`; new natural hooks ran.

The same experiment installed the initial marketplace with `--ref v0.0.1`,
advanced its Git main branch to `0.0.2`, and ran native marketplace refresh.
The installed version and the fetched catalog payload both stayed at `0.0.1`.
Immutable Release tags therefore prevent the initial native Git source from
following an unselected latest branch.

Native source objects use `source: "git-subdir"` with `url`, `path`, and `sha`.
An earlier fixture exposed that `source: "git"` is skipped by these native
versions; the product was corrected and tested through native listing/install.
A local catalog source never participates in native Git-marketplace refresh.

## Native turn interruption

The [MCP cancellation specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/cancellation)
defines cancellation as optional. The gap below fails MindIE's required native
interruption behavior; it is not a claim that the host violates a mandatory MCP
feature.

The standard notification gap was observed on 2026-10-16. In an isolated
macOS Codex `0.153.4` experiment, `turn/interrupt` was acknowledged, the native
turn became `interrupted`, and the host remained responsive. However, a pending
`remote_read` operation's helper and SSH subtree remained alive throughout the
8.03-second observation. The task-owned listener never supplied an SSH banner,
so no authentication or remote command occurred. The experiment used loopback
synthetic Responses and no external model requests.

A transparent observer recorded the actual integer `tools/call` request ID and
no `notifications/cancelled` on either MCP connection during that interval.
Cleanup completed after the listener and native host were closed. The observer
was then removed and the original plugin declaration restored.

The exact upstream
[MCP tool path](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/mcp_tool_call.rs)
uses the cancellation token during approval without passing it to the approved
call. The corresponding
[legacy client request](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/rmcp-client/src/rmcp_client.rs)
awaits the request handle's response. This agrees with the missing standard
cancellation notification observed at the adapter boundary.

The adapter handles `notifications/cancelled` for the matching pending request;
`test_mcp_protocol_call_and_cancellation` tests that server-side contract. It
does not prove that native turn interruption sends the notification. The
`native.legacy-mcp` registry entry continues to describe configuration/loading
support, not successful native cancellation. Separate child-thread experiments
on both declared versions also observed missing notification delivery, as
detailed below. No polling supervisor or automatic operation replay is provided
as a substitute for missing host delivery. The native Hook route below
addresses the confirmed main-thread delivery gap without relying on that
notification.

## Native Interrupt Hook

The plugin declares native `Interrupt` hooks of type `mcp_tool` for both existing
MCP servers. They call the unadvertised `mindie_interrupt` control on the same
connection, with `${session_id}` and `${turn_id}` from the native Hook event.
The control checks the native `_meta.threadId` against its argument, then signals
only pending requests whose independently validated native thread and turn both
match. It runs on the MCP reader before the worker capacity gate, so occupied
tool slots cannot block cancellation. It reuses the same cancellation Events
and owned-process cleanup as standard `notifications/cancelled`.

A neutral Hook result confirms that matching calls were signalled, not that
their business effects were undone or that cleanup completed. Each original
call retains its own outcome and cleanup evidence. Malformed or contradictory
control identities fail without signalling any request. Neither requests in
other threads nor later turns are cancelled, and no operation is replayed.
The control is not exposed to the model through `tools/list`.

Both exact supported source revisions contain `events/interrupt.rs` and
`engine/mcp_runner.rs`, including turn input and MCP argument-template expansion.
An isolated native `0.153.4` prototype confirmed hidden control dispatch to the
existing connection while a real SSH call was pending. The input identity
matched the pending call; the Hook's metadata contained `threadId` but no nested
turn metadata. Translating the control to the existing cancellation path
removed the owned helper/SSH subtree in 70.33 ms, with the native host still
responsive and the stalled listener open. The experiment used no external
model and restored its task configuration. This prototype is interface evidence;
the final product wiring has separate component and native acceptance records.

The product implementation was then exercised on the same exact native version
with a task-local overlay of the candidate MCP entry and the original installed
runtime. Both native Interrupt handlers completed, including the knowledge
connection with no pending call. A passive wire observer forwarded bytes without
creating or translating cancellation messages. The remote control signalled the
product's existing Event; the actual helper and SSH subtree exited in 73.40 ms
while the stalled listener remained open. The host reported `interrupted` and
remained responsive. All three task configuration files were restored exactly;
no external model, summary, authentication or remote command occurred. This
proves the product cancellation route, separately from installation of a final
locked package on each release platform.

Candidate `11cdc0aee8cd09213ba40963a46218fbb3dc1314` was subsequently installed
as a noneditable locked package on all three platforms. Each run checked 64
installed product files, used the actual native Interrupt hooks without a trust
bypass, and observed the pending owned operation disappear while the listener
and host remained open:

| Platform | Native version | Observed cleanup after interrupt | Owned processes |
|---|---|---|---|
| macOS | `0.153.4` | 76.41 ms | 6 |
| Native Windows | `0.153.4` | 113.82 ms | 5 |
| WSL Ubuntu 24.04 | `0.153.4` | 65.81 ms | 6 |

These are individual lifecycle observations, not latency guarantees. Both Hook
handlers completed; the host remained responsive and exited normally; exact
service retirement and fixture cleanup completed. Each run used one loopback
synthetic response and no external model request. No SSH banner, authentication
or remote command occurred. The original Windows fixture failed before SSH
because it used a local Windows path as the remote POSIX root; that failure was
preserved, and the corrected fixture used a new output directory with the same
installed candidate.

Native `Interrupt` is a main-thread event: the documented contract excludes idle
threads and subagents. This route therefore does not claim subagent interrupt
delivery. A separate native Windows `0.162.0-alpha.17.2` interface fixture
also confirmed the hidden control on the existing connection, exact input
thread/turn, `threadId` metadata and a responsive interrupted host. It used a
minimal MCP stub, so it does not prove cleanup through the final installed
MindIE package. Exact Hook approvals use the existing native
trust API; only previously enabled and trusted own handlers inherit approval,
and an explicit disablement or revoked approval is preserved.

## Child-agent cancellation remains a release gate

Separate native experiments on macOS `0.153.4` and native Windows
`0.162.0-alpha.17.2` spawned a child with its own pending MCP request, then
interrupted that child directly. The child interrupt was acknowledged and the
turn became `interrupted`, but the observed connection received neither
`notifications/cancelled` nor stdin EOF during the following 8.1 and 8.6 seconds,
respectively. These were interface fixtures with no external model requests;
they do not establish cancellation through a final MindIE child invocation.
The parent spawn turn had already completed, so neither experiment proves
propagation from an active parent's interruption.

The alpha receipt was corrected to align the final child thread, request ID,
connection and MCP PID. Its original process-list parser dropped every row, so
the recorded empty process lists do not establish process absence or cleanup
at the event boundary. Independent later CIM readback found neither recorded
PID nor a remaining process for the cell; this proves only that later state.
The separate alpha `close_agent` exploration repeated calls because of a fixture
loop defect and has no valid one-shot close receipt. It is not a substitute for
the child-interrupt test.

Both exact source revisions explicitly skip `Interrupt` for subagents.
`SubagentStop` can run in the child's execution context, but is dispatched on
normal completion; the `TurnAborted` path does not dispatch it. `SessionEnd` is
root-only, and these revisions have no `StopFailure` event. These Hooks therefore
cannot deliver the missing child cancellation signal. The locked SDK exposes
explicit request cancellation but does not automatically cancel when the
response future is dropped.

The minimum upstream repair belongs at the Harness's submitted MCP request
boundary: retain the exact connection/request ID, disarm on a consumed terminal
response, and send standard cancellation when the owning future is dropped.
Legacy calls and modern continuations must use the same ownership rule. A
task-local proposal is under review; no installed Harness was patched and it
does not close this gate. Notification dispatch is not proof of rollback or
server-side cleanup. Until that native behavior is implemented and verified,
the main-thread passes must not be described as all-agent cancellation support.

## Runtime and ownership boundary

The host owns plugin install/cache/config facts. uv owns complete locked
noneditable environments. The adapter keeps only immutable runtime receipts,
one pending native-operation receipt, and the existing canonical adapter
configuration. Its status timestamp is throttling data, never selection authority.
An already loaded facade dispatches a complete current interpreter/helper pair;
it does not import a new shared library into the old facade process.

Prepared runtime, native cache presence, enabled configuration and component
tests do not establish natural Stop capture/index/publication acceptance.
The release matrix records those separately.
