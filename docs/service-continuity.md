# Installed Service Identity & Continuity

<a href="service-continuity.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

`mindie-agent-codex` separates plugin distribution from runtime execution: OpenAI Codex CLI manages the plugin catalog and installed state, while `uv` builds an immutable locked Python runtime outside the host's disposable cache.

---

## 1. Immutable Runtime Outside Disposable Host Caches

- **Why Fixed Entries Matter**: When Codex refreshes a marketplace plugin, it can evict the previous cache directory while the running `app-server` still holds existing hook and MCP references. Placing the prepared release entry and `uv` runtime under persistent `PLUGIN_DATA` ensures active sessions never break mid-flight.
- **Single Canonical Binding**: The user adapter configuration file (`codex.json`, resolved across Windows `%APPDATA%/mindie-agent` and POSIX `XDG_CONFIG_HOME`) is the single source of truth for the active runtime binding and content-addressed 9-domain `domain_configs` (`engines/codex/<20hex>/<domain>.engine.json`). Loaded entrypoints read that canonical profile and dispatch the complete interpreter + helper pair together, never mixing modules across versions.
- **Stable Hook Trust Hashes & Immutable Native Payload**: Keeping hook command declarations byte-frozen across versions—while launching source Python entrypoints with `-B` and running baseline `uv sync` against a short-lived temporary staging copy (`mindie-build-*` in `%TEMP%`) so `.pyc`, `build/`, and `*.egg-info` artifacts never touch the native plugin cache—allows `app-server` to preserve user hook trust approvals across cold startup and updates without dropping `SessionStart`, `UserPromptSubmit`, `Stop`, or `Interrupt` execution.

---

## 2. Shared Operation Locking & Atomic Idle Cutover

- **Shared Lock for Active Requests**: Long-lived MCP processes read the active runtime binding under shared generation leases before each request, ensuring in-flight calls run to completion on a consistent runtime while leaving `Active + Rollback <= 2` runtime generation GC solely to `mindie-harness` (never deleting `<data>/runtimes/codex/` directories from `bootstrap.py`).
- **Marker-Only `SessionStart` & Background Candidate Preparation**: Cold and warm `SessionStart` write only `pending-update-check.json` in `ProfileRef.state_dir` and exit `{}` in `< 30ms` without synchronous `uv sync` or SQLite creation. Background maintenance via `mindie_harness.runtime.wheelhouse.update_runtime` checks for compatible plugin-release updates and formal shared-component Release updates across all 9 runtime packages, staging candidates side by side outside the operation lock while preserving installed component version floors and running a model-free probe without starting a knowledge worker.
- **Exclusive Switch Under `.codex.json.update.lock`**: Activating or rebinding a runtime acquires `.codex.json.update.lock` exclusively, delegates 9-domain content-addressed engine provisioning and previous service retirement to `ensure_knowledge_binding(..., retire_previous=True, already_locked=True)`, and atomically updates `codex.json` only after retirement and binding succeed (`status == "configured"`). If the service is busy, activation is deferred without interrupting user work.

---

## 3. Decoupled User Authorization

- Community knowledge sharing and automatic tool fault reporting share a single unified contribution switch in `mindie-consent.json` beside `codex.json`, stored alongside approved project roots as persistent user data decoupled from code versions.
- Explicitly enabling (`contribute` with the current unified consent revision) configures knowledge sharing and starts the background reporter, keeping three states distinct: (1) local consent configuration saved, (2) background reporter worker running, and (3) external GitHub Issue/PR created.
- Updating or reloading the plugin never re-prompts for consent, modifies saved `disabled` / `later` / `read-only` choices, or silently upgrades legacy knowledge-only `contribute` records lacking the unified consent revision (`needs_choice`).

## 4. First Migration from 0.1.8 / 0.1.9

When the installed MCP seed is newer than the canonical binding, the 0.1.10 launcher enters its new migration controller, waits for old operations to release the operation lock, and publishes the complete binding through the existing atomic activation path. Concurrent MCP startup shares bounded waits, including macOS file-lock contention. Hook commands remain stable, and Stop never starts migration.

Already-running 0.1.8 / 0.1.9 code cannot acquire this fix by itself. If its updater is stuck on an activation receipt, the first recovery requires the native installer to select the exact source of the formal 0.1.10 Release, followed by a host-supported MCP refresh or natural reload. Repeating `plugin add` against a marketplace still pinned to an old candidate installs that old version again. Editing the canonical version, removing lock files, or overwriting runtime files is not a migration.

Native installation, runtime preparation, canonical activation, and host loading of new tools are separate verification points. Completed obsolete candidates can be retired from their receipts after native cache eviction. Interrupted local activation uses an immutable attempt snapshot and canonical readback rather than a permanent old `unknown` block. Unconfirmed native installation or marketplace writes remain unreplayed.
