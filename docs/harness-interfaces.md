# Codex Harness interface evidence

The machine-checked registry is `plugins/mindie-agent/harness-compatibility.json`.
It declares two singleton intervals: Codex `0.153.4` and
`0.162.0-alpha.17.2`. The declaration does not claim continuous support between
those endpoints. CI resolves each registered production symbol and test method,
then uses the shared metadata validator to check every declared interval against
every used interface.

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

Native turn interruption is an open release gate as of 2026-10-16. In an isolated
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
support, not successful native cancellation. This observation does not classify
`0.162.0-alpha.17.2` as failed: that binary has not undergone this interrupt
experiment. No polling supervisor or automatic operation replay is provided
as a substitute for missing host delivery.

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
