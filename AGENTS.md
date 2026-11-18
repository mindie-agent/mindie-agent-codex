# MindIE Agent — Codex Host Plugin (`mindie-agent-codex`)

This repository implements the native OpenAI Codex plugin (`plugins/mindie-agent`) for MindIE Agent, backed by `mindie-harness` (`API_VERSION = 1`) and the shared runtime components (`knowledge`, `remote-dev`, `diagnostics`, `debugger`, `profiler`, `sim`, `coordinator`, `npu-top`).

## 1. Frozen Host Contracts & Boundaries

- **Byte-Frozen Hook Manifest (`HOST-NATIVE`)**: `plugins/mindie-agent/hooks/hooks.json` is trust-pinned by Codex (`25,650` bytes, `sha256 == ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`). Never alter hook shell commands or `hooks/hooks.json` without explicit architecture approval.
- **Silent Default-On MCP Surfaces (`HOST-ADMISSION`)**:
  - Public MCP servers in `.mcp.json` (`mindie-remote-dev`, `mindie-tools`) work immediately in CLI, Desktop, Work mode, Automations, and Subagents with zero prompt-prefix gates, `x-codex-turn-metadata`, or `request_nonce`.
  - Knowledge tools (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) are exposed by default on `mindie-tools` (`tools-mcp`); `Stop` checks community sharing consent + `project_roots` before touching SQLite.
  - The Codex `remote-dev` profile exposes exactly 10 tools (`remote_exec_command`, `remote_write_stdin`, `remote_view_image`, `remote_apply_patch`, `remote_auth`, `remote_probe`, `remote_artifact_pull`, `remote_artifact_push`, `remote_job_status`, `remote_job_stop`); read-only calls run concurrently without mutation-slot throttling or hot-path `.owner.lock` scans (`AD-09`).
- **Thin Host Plugin Delegation (`HOST-WIRE`)**:
  - Bundle deterministic `assets/{mindie-harness-seed.pyz, seed-lock.json}` and delegate shared RSI, knowledge RPC, file-locking, MCP serving, and `HostAdapter` contracts to `mindie_harness`.
  - Keep the entire `plugins/mindie-agent/scripts/` surface `<= 1,100` lines with a top-30-line `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]` docstring header on every module.
- **State Honesty & Upgrade Continuity (`HOST-UPGRADE`, Principles 9, 10, 14, 18)**:
  - Distinguish `completed`, `failed`, and `unknown` external effects; never swallow `TimeoutError` inside `except OSError:`.
  - Once a primary remote/component call succeeds, local receipt accounting errors must preserve `isError=False` (`operation_outcome="preserved"`).
  - Preserve backward-compatible `codex.json`, `.prepare-codex.lock`, and `releases/<ver>/runtime.json` layouts across upgrades and rollbacks (`<= 2` active + rollback runtime generations).

## 2. Verification Commands

Run all tests inside an isolated environment (`HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_*`, `MINDIE_*`, `REMOTE_DEV_*`, `UV_CACHE_DIR`, `TMPDIR` isolated via `tests/conftest.py`):

```bash
python3 .github/scripts/check_lf_eol.py
pytest -q tests/
python3 plugins/mindie-agent/scripts/setup.py release-metadata --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"
```
