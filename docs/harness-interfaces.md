# Codex Harness Interface Evidence

<a href="harness-interfaces.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

[`plugins/mindie-agent/harness-compatibility.json`](../plugins/mindie-agent/harness-compatibility.json) is the machine-checked registry of OpenAI Codex CLI interfaces used by `mindie-agent-codex`. Release `0.0.2` declares support for two exact verified Codex versions:
- **`0.153.4`** (official upstream commit `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`)
- **`0.162.0-alpha.17.2`** (official upstream commit `740e5af33c71225640e0c1c1555c514c2c93ab74`)

In CI, `plugins/mindie-agent/scripts/release_metadata.py` verifies that every registered production symbol and contract test exists and validates the declared version intervals against all consumed interfaces.

---

## Native management

Both verified Codex revisions expose `hooks/list`, `config/read`, and `config/value/write` in `codex-rs/app-server-protocol/src/protocol/common.rs`, providing hook identity, content hash, enabled state, trust state (`hooks.state.<key>.trusted_hash`), and optimistic compare-and-swap (`expectedVersion`).

### How Native Marketplace & Hook Trust Updates Work
- **Immutable Tag Pinning**: Installing the marketplace with `--ref v0.0.2` pins the catalog to that immutable Release tag so a later `main` branch update never silently upgrades an unverified version.
- **Git Subdirectory Source**: Native catalog entries use `source: "git-subdir"` with `url`, `path`, and `sha` (tested through native `codex plugin list` and `codex plugin add`).
- **Preserving User Hook Approvals**: During a compatible silent update, `native_hooks.py` updates only this marketplace's source via optimistic CAS (`expectedVersion`), installs the selected subdirectory at its exact commit SHA, and carries forward trust hashes **only** for `SessionStart` / `Stop` hooks that the user had already enabled and trusted. Explicitly disabled or untrusted hooks remain disabled, and unrelated plugins are never touched.

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
