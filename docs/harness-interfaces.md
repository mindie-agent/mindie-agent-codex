# Codex Harness Interface Evidence

<a href="harness-interfaces.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

[`plugins/mindie-agent/harness-compatibility.json`](../plugins/mindie-agent/harness-compatibility.json) is the machine-checked registry of OpenAI Codex CLI interfaces used by `mindie-agent-codex`. The registry includes the previously verified hosts and two additional stable versions, `0.162.1` and `0.164.0` (the latest official stable release checked on 2026-10-26). Each is an exact version; untested patch releases and prereleases remain excluded.

On WSL Linux, both added versions pass `tests/test_native_harness_acceptance.py` against the real executable: local marketplace installation and readback, all five bundled hooks, config compare-and-swap, preservation of trusted and explicitly disabled handlers, locked runtime preparation, a native SessionStart, and discovery of 22 remote tools and 5 component tools without enabling knowledge capture. CI repeats these model-free checks on Ubuntu for both versions. This evidence does not claim paid model-turn, remote NPU, or Windows/macOS acceptance for the newly added hosts.

The official source for [0.162.1](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/mcp_tool_call.rs) and [0.164.0](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/core/src/mcp_tool_call.rs) retains the native thread and turn metadata used by the adapter. Existing protocol tests continue to cover identity rejection and targeted cancellation.

In CI, `plugins/mindie-agent/scripts/release_metadata.py` verifies that every registered production symbol and contract test exists and validates the declared version intervals against all consumed interfaces.

---

## Native management

### Codex 0.166 acceptance

Release 0.1.15 adds the exact versions `0.166.0` and
`0.166.0-alpha.5.1`. On 2026-10-31 the actual Windows Desktop executable passed
the isolated `tests/test_native_harness_acceptance.py` checks: native plugin
installation, five hooks, trust/config CAS, explicit disable preservation,
locked runtime setup, SessionStart and discovery of 23 remote tools and five
component tools. The old `no_compatible_release` result reflected an absent
declaration; no break was observed in these consumed host interfaces.

The Windows run also exposed a separate Python resource-path failure in a
deep plugin-data directory. New runtime interpreters retain Windows extended
paths in `sys.prefix`; the same native acceptance now passes. Stable 0.166.0
joins the Ubuntu native CI matrix and gains a Windows native job. Existing
contract tests cover the remaining registered boundaries. This evidence does
not claim paid model turns, NPU execution, or compatibility with future hosts.

The public [hook](https://learn.chatgpt.com/docs/hooks) and
[app-server](https://learn.chatgpt.com/docs/app-server) contracts describe the
interfaces exercised by these checks.

Both verified Codex revisions expose `hooks/list`, `config/read`, and `config/value/write` in `codex-rs/app-server-protocol/src/protocol/common.rs`, providing hook identity, content hash, enabled state, trust state (`hooks.state.<key>.trusted_hash`), and optimistic compare-and-swap (`expectedVersion`).

### How Native Marketplace & Hook Trust Updates Work
- **Immutable Tag Pinning**: Installing the marketplace with `--ref v0.0.2` pins the catalog to that immutable Release tag so a later `main` branch update never silently upgrades an unverified version.
- **Git Subdirectory Source**: Native catalog entries use `source: "git-subdir"` with `url`, `path`, and `sha` (tested through native `codex plugin list` and `codex plugin add`).
- **Preserving User Hook Approvals**: During a compatible silent update, `native_hooks.py` updates only this marketplace's source via optimistic CAS (`expectedVersion`), installs the selected subdirectory at its exact commit SHA, and carries forward trust hashes **only** for `SessionStart`, `UserPromptSubmit`, `Stop`, and `Interrupt` hooks that the user had already enabled and trusted. Explicitly disabled or untrusted hooks remain disabled, and unrelated plugins are never touched.

---

## Native turn interruption

While the [MCP specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/cancellation) defines `notifications/cancelled` as optional, an interrupted turn in Codex `0.153.4` (`turn/interrupt`) does not forward a standard MCP cancellation notification to an already-approved in-flight `tools/call` request (see upstream [`mcp_tool_call.rs`](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/mcp_tool_call.rs) and [`rmcp_client.rs`](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/rmcp-client/src/rmcp_client.rs)).

To ensure local helper and SSH subprocesses are cleaned up immediately when a user interrupts a main-thread turn—without introducing polling supervisors or arbitrary timeouts—`mindie-agent-codex` uses the native `Interrupt` hook described below alongside standard MCP cancellation and stdin `EOF` cleanup.

---

## Native Interrupt Hook

`mindie-agent-codex` registers native `Interrupt` hooks of type `mcp_tool` on both MCP connections (`mindie-knowledge` and `mindie-remote-dev`):
- **Targeted In-Flight Cancellation**: When the user interrupts the active main-thread turn, the hook invokes the internal `mindie_interrupt` control on the existing MCP connection with `${session_id}` and `${turn_id}`.
- **Bypassing Worker Queue Saturation**: The control handler runs directly on the MCP reader before the worker capacity gate, verifies `_meta.threadId`, and signals only in-flight requests matching both the native thread and turn ID.
- **Fast Subprocess Cleanup**: Signalling triggers the same deterministic process-tree cleanup used by `notifications/cancelled`. Across locked non-editable installations on `0.153.4`, interrupting a pending SSH tool call cleaned up all owned local helper/SSH processes while keeping the MCP server and Codex host responsive:

| Platform | Verified Codex Version | Observed Cleanup After Interrupt | Reclaimed Local Processes |
|---|---|---|---|
| macOS | `0.153.4` | 76.41 ms | 6 |
| Native Windows | `0.153.4` | 113.82 ms | 5 |
| WSL Ubuntu 24.04 | `0.153.4` | 65.81 ms | 6 |

- **Separation from Durable Remote Jobs**: Cleaning up local SSH transport processes does not claim that an already-started remote workload rolled back; persistent background jobs created via `remote-dev` retain their own job ID and lifecycle.

---

## Known limitation: child-agent cancellation

In release `0.0.2`, Codex's native `Interrupt` event is dispatched only for main-thread turns and explicitly skips subagents spawned via `spawn_agent` (and `SubagentStop` fires only on normal completion, not on `TurnAborted`).

- **Impact**: If a spawned child agent is waiting on an in-flight MCP tool call when interrupted, neither `notifications/cancelled` nor the native `Interrupt` hook is delivered to that child MCP call. Its pending local helper/SSH process—or an already-started remote command—may continue running until the host process exits.
- **Design Stance**: Following the [MindIE Agent Engineering Design Principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.en.md), the plugin documents this upstream host limitation transparently rather than shipping a custom-patched Codex binary, polling process killer, or arbitrary business timeout. Upstream tracking and reproduction evidence are posted in [openai/codex#26956](https://github.com/openai/codex/issues/26956).

---

## Runtime & Ownership Boundary

- **Host vs. Runtime**: Codex owns plugin installation records and cache directories; `uv` builds immutable locked Python environments outside the disposable plugin cache; and the adapter configuration file is the single source of truth for the active runtime binding.
