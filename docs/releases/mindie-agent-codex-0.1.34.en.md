# mindie-agent-codex 0.1.34: Storage Lifecycle Governance and Component Baseline Alignment

[中文](mindie-agent-codex-0.1.34.md)

- **Wave 1 formal component baseline (`plugins/mindie-agent/pyproject.toml`, `uv.lock`)**: Pins `mindie-knowledge`, `mindie-diagnostics`, `remote-dev`, `mindie-coordinator`, `mindie-profiler`, `mindie-sim`, and `mindie-debugger` to the reviewed formal-release commits, and regenerates `mcp_catalog.json` so discovery and dispatch come from the same runtime.

- **A single managed `.venv` on upgrade (`bootstrap.py`, `component_update.py`)**: When the installed configuration already carries a component floor and its interpreter is healthy, that interpreter first probes/builds the merged component runtime. A produced `component-runtimes/<key>` is returned directly, skipping the orphan baseline runtime that would otherwise be built and immediately superseded. A single baseline is built only when no component override is needed, so upgrades no longer consume double disk; switching to the baseline removes only the old floor's managed component commands and leaves user custom wrappers byte-for-byte unchanged.

- **Silent reclamation of historical baggage (`bootstrap.py`, `component_update.py`, `release_activation.py`, `harness_update.py`, `session_maintenance.py`, `storage_gc.py`)**: Before a build, after a switch, and during session maintenance, bounded calls to shared `mindie_knowledge.storage` `sweep_legacy_installations` and `prune_component_runtimes` reclaim 10+ generations of `component-runtimes`, `runtimes`, old `releases/<old_ver>`, excess `updates/superseded-*.json`, and `prepare-*.log`. The current binding, the previous rollback generation, recently verified generations, live lease/process references, and cross-plugin borrowed generations are always retained. A cleanup failure records `cleanup_warning` / `deferred_in_use` and never changes an already successful primary flow.

- **Resident MCP holds a shared runtime lease (`entry.py`)**: `knowledge-mcp`, `remote-mcp`, `tools-mcp`, and `bridge mcp` hold `acquire_runtime_lease` on their owning managed generation while serving, so background reclamation on POSIX and Windows never moves a runtime serving a live session.

- **Same-volume uv cache and bounded installer logs (`bootstrap.py`, `storage_gc.py`)**: When the caller has not set `UV_CACHE_DIR` and uv's default cache is on another volume, a same-volume managed `<data>/.uv-cache` is used. A successful install removes its own `prepare-*.log` immediately; failures retain only a bounded recent set of diagnostic logs.

- **`RemoteReceipts` owner-lock and capture `.active` reclamation (`mcp_gate.py`, `receipt_owners.py`, `capture_spool.py`, `mcp_transport.py`)**: Releasing `_ownership` removes its own `<owner>.owner.lock` after a non-blocking exclusive probe proves no contention, and initialization performs a bounded sweep of historical dead-owner lock files. A `Capture` reclaims stale orphan `*.active` files older than 24h that are not open in this process, and `_abandon()` removes its own created-but-unused temporary file.

- **Cross-platform guarantees**: Windows never calls `os.kill(pid, 0)`, no open handle inside a target directory survives an NTFS rename, path equivalence uses `mindie_knowledge.filesystem.path_identity`, and every text file keeps Unix `LF`.

- **Zero pre-call turn-identity gates on deterministic surfaces (`P2.7`)**: `components` and `remote` execute without mandatory `_meta` turn metadata via connection-scoped fallback (keeping `knowledge` strictly gated on `native_turn_identity` / `P2.1`), and `live_catalog.py` normalizes partial `x-codex-turn-metadata` with `thread_id` when forwarding to `coordinator`.
