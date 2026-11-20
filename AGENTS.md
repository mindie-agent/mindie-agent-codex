# MindIE Agent — Codex Host Plugin (`mindie-agent-codex`)

This repository implements the native OpenAI Codex plugin (`plugins/mindie-agent`, `v0.1.40`) for MindIE Agent, backed by `mindie-harness` (`>= 0.1.5`, `API_VERSION = 1`) and all 9 shared runtime packages (`mindie-harness`, `mindie-knowledge`, `remote-dev`, `mindie-diagnostics`, `mindie-coordinator`, `mindie-debugger`, `mindie-profiler`, `mindie-sim`, `npu-top`).

## 1. Frozen Host Contracts & Boundaries

- **Byte-Frozen Hook Manifest (`HOST-NATIVE`)**: `plugins/mindie-agent/hooks/hooks.json` is trust-pinned by Codex (`25,650` bytes, `sha256 == ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`). Never alter hook shell commands or `hooks/hooks.json` without explicit architecture approval.
- **Silent Default-On MCP Surfaces & Fast Hooks (`HOST-ADMISSION`)**:
  - Public MCP servers in `.mcp.json` (`mindie-remote-dev`, `mindie-tools`) resolve both Windows `%APPDATA%/mindie-agent/codex.json` and POSIX `XDG_CONFIG_HOME` fallbacks cleanly and work immediately in CLI, Desktop, Work mode, Automations, and Subagents with zero prompt-prefix gates, `x-codex-turn-metadata`, or `request_nonce`.
  - Cold and warm `SessionStart` write only `pending-update-check.json` in `ProfileRef.state_dir` and exit `{}` in `< 30ms` without synchronous `uv sync` or SQLite creation.
  - Knowledge tools (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) are exposed by default on `mindie-tools` (`tools-mcp`) alongside `mindie_status`, `mindie_doctor`, `profiler`, `sim`, `coordinator`, `npu_top`, `debugger`, `rsi_loop`, `experiment_ledger`, `diagnostics_status`, and `diagnostics_bundle`. `Stop` checks community sharing consent + `project_roots` before touching SQLite and delegates authorized turns to `run_stop_capture(..., timeout_sec=6.0)` with honest `turn` vs `notification` identity.
  - The Codex `remote-dev` profile exposes the 10 Codex tools (`remote_exec_command`, `remote_write_stdin`, `remote_view_image`, `remote_apply_patch`, `remote_auth`, `remote_probe`, `remote_artifact_pull`, `remote_artifact_push`, `remote_job_status`, `remote_job_stop`); read-only calls run concurrently without mutation-slot throttling or hot-path `.owner.lock` scans (`AD-09`).
- **Thin Host Plugin Delegation (`HOST-WIRE`)**:
  - Bundle deterministic `assets/{mindie-harness-seed.pyz, seed-lock.json}` and delegate shared RSI, 9-domain content-addressed `engines/codex/<20hex>/<domain>.engine.json` provisioning (`ensure_knowledge_binding(..., already_locked=True)`), `Stop` capture (`run_stop_capture`), file-locking (`.codex.json.update.lock`), MCP routing (`MCPRouter` + `ManagedMCPAdapter`), and `HostAdapter` contracts to `mindie_harness`, keeping turn-scoped `(thread_id, turn_id)` `mindie_interrupt` correlation in `CodexHostAdapter`.
  - Keep the entire `plugins/mindie-agent/scripts/` surface `<= 1,100` lines (`1,094` lines in `v0.1.40`) with a top-30-line `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]` docstring header on every module.
- **State Honesty & Upgrade Continuity (`HOST-UPGRADE`, Principles 9, 10, 14, 18)**:
  - Distinguish `completed`, `failed`, and `unknown` external effects; never swallow `TimeoutError` inside `except OSError:`.
  - Once a primary remote/component call succeeds, local receipt accounting errors must preserve `isError=False` (`receipt_warning` + `receipt_unconfirmed=True`).
  - Preserve backward-compatible `codex.json`, `.codex.json.update.lock`, `.prepare-codex.lock`, and `releases/<ver>/runtime.json` layouts across upgrades and rollbacks while leaving runtime generation GC (`Active + Rollback <= 2`) solely to `mindie-harness`.

## 2. Verification Commands

Run all tests inside an isolated environment (`HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_*`, `MINDIE_*`, `REMOTE_DEV_*`, `UV_CACHE_DIR`, `TMPDIR` isolated via `tests/conftest.py`):

```bash
uv run --no-project python .github/scripts/check_lf_eol.py
uv run pytest -q tests/
uv run --no-project python plugins/mindie-agent/scripts/setup.py release-metadata --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"
```
