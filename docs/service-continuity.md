# Installed Service Identity & Continuity

<a href="service-continuity.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

`mindie-agent-codex` separates plugin distribution from runtime execution: OpenAI Codex CLI manages the plugin catalog and installed state, while `uv` builds an immutable locked Python runtime outside the host's disposable cache.

---

## 1. Immutable Runtime Outside Disposable Host Caches

- **Why Fixed Entries Matter**: When Codex refreshes a marketplace plugin, it can evict the previous cache directory while the running `app-server` still holds existing hook and MCP references. Placing the prepared release entry and `uv` runtime under persistent `PLUGIN_DATA` ensures active sessions never break mid-flight.
- **Single Canonical Binding**: The user adapter configuration file is the single source of truth for the active runtime binding. Loaded entrypoints read that canonical profile and dispatch the complete interpreter + helper pair together, never mixing modules across versions.
- **Stable Hook Trust Hashes**: Keeping hook command declarations stable across versions allows `app-server` to preserve user hook trust approvals across updates without silently dropping `SessionStart` or `Stop` execution.

---

## 2. Shared Operation Locking & Atomic Idle Cutover

- **Shared Lock for Active Requests**: Long-lived MCP processes read the active runtime binding under a shared operation lock before each request, ensuring in-flight calls run to completion on a consistent runtime.
- **Background Candidate Preparation**: Compatible update candidates are built in isolation without holding the operation lock.
- **Exclusive Switch Only When Idle**: Activating a new runtime acquires the exclusive lock only when the service is idle, retires the exact previous service owner, verifies the new owner's readiness, and updates the canonical binding atomically. If the service is busy, activation is deferred without interrupting user work.

---

## 3. Decoupled User Authorization

- Community sharing consent, approved project roots, and product fault-reporting preferences are persistent user data decoupled from code versions.
- Updating or reloading the plugin never re-prompts for consent or modifies saved opt-out choices.
