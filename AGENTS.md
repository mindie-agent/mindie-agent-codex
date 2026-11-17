# MindIE Agent — Codex Host Plugin (`mindie-agent-codex`)

This repository implements the native OpenAI Codex plugin (`plugins/mindie-agent`) for MindIE Agent, backed by `mindie-harness` (`API_VERSION = 1`) and the shared runtime components (`knowledge`, `remote-dev`, `diagnostics`, `debugger`, `profiler`, `sim`, `coordinator`, `npu-top`).

## 1. Frozen Host Contracts & Boundaries

- **Byte-Frozen Hook Manifest (`HOST-NATIVE`)**: `plugins/mindie-agent/hooks/hooks.json` is trust-pinned by Codex (`25,650` bytes, `sha256 == ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`). Never alter hook shell commands or `hooks/hooks.json` without explicit architecture approval.
- **MCP Surfaces & Zero-Gate Deterministic Tools (`HOST-ADMISSION`, `P2.7`)**:
  - Public MCP servers in `.mcp.json` (`mindie-remote-dev`, `mindie-tools`) work immediately in CLI, Desktop, Work mode, Automations, and Subagents without `/mindie-agent`, `x-codex-turn-metadata`, or `request_nonce`.
  - Knowledge tools (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`) remain strictly private to explicit `/mindie-agent` or `$mindie-agent` sessions (`P2.1`) via `knowledge_cli.py`.
  - Read-only remote tools (`remote_read`, `remote_grep`, `remote_glob`, `remote_stat`, `remote_ls`, `remote_job_status`, `remote_job_output`, `remote_job_list`) run concurrently without mutation-slot throttling or hot-path `.owner.lock` scans (`AD-09`).
- **Thin Host Plugin Delegation (`HOST-WIRE`)**:
  - Bundle deterministic `assets/{mindie-harness-seed.pyz, seed-lock.json}` and delegate shared RSI (`rsi_loop.py`, `rsi_explore.py`, `experiment_ledger.py`), file-locking, and `HostAdapter` contracts to `mindie_harness`.
  - Keep core Python modules `< 800` lines with a top-30-line `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]` docstring header.
- **State Honesty & Upgrade Continuity (`HOST-UPGRADE`, Principles 9, 10, 14, 18)**:
  - Distinguish `completed`, `failed`, and `unknown` external effects; never swallow `TimeoutError` inside `except OSError:`.
  - Once a primary remote/component call succeeds, local receipt accounting errors must preserve `isError=False` (`operation_outcome="preserved"`).
  - Preserve backward-compatible `codex.json`, `prepare.lock`, and `releases/<ver>/runtime.json` layouts across upgrades and rollbacks (`<= 2` active + rollback runtime generations).

## 2. Verification Commands

Run all tests inside an isolated environment (`HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_*`, `MINDIE_*`, `REMOTE_DEV_*`, `UV_CACHE_DIR`, `TMPDIR` isolated via `tests/conftest.py`):

```bash
python3 .github/scripts/check_lf_eol.py
pytest -q tests/
python3 plugins/mindie-agent/scripts/release_metadata.py --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"
```
