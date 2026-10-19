# MindIE Agent — Codex Native Harness Plugin (`mindie-agent-codex`)

This repository implements the **Codex Native Harness Plugin** (`plugins/mindie-agent/`) for MindIE Agent. It bridges the Codex CLI host lifecycle (Hooks, Skills, and stdio MCP server) to the shared runtime components (`mindie_knowledge`, `remote_dev`, `mindie_diagnostics`, `npu_top`, and `coordinator`) without duplicating core domain engines.

---

## 1. Plugin Script Navigation & Call Graph

All plugin scripts live under `plugins/mindie-agent/scripts/` and include standardized top-of-file Agent Contract docstrings (`[Role]`, `[Invariants]`, `[Timeouts & Locks]`, `[Side-Effect Policy]`):

| Layer | Script | Role & Key Responsibilities |
| :--- | :--- | :--- |
| **Native Entry & Bootstrap** | `native_entry.py` | Renders `hooks/hooks.json` and `.mcp.json` commands using an inline Python launcher that delegates to `bootstrap.py`. |
| | `bootstrap.py` | Resolves/prepares the isolated Python runtime (`bootstrap_runtime.py`) and dispatches `entry`, `mcp-gate`, `remote-bridge`, or `bridge`. |
| **Host Hook Lifecycle** | `entry.py` | Explicit `/mindie-agent` entry controller (`enter`, `status`, `leave`, `stop`, `user_prompt_submit`). Returns `status="degraded"` (exit 0) if service binding fails after lease activation. |
| | `bridge.py` | CLI & Hook dispatcher for retrieval, feedback, capture (`stop`), status, and community sharing toggles. Returns `status="degraded"` when capture fails so active leases stay intact. |
| | `session_gate.py` / `admission_ops.py` | Entry-bound task lease management backed by `mindie_knowledge.loop.activation.Admission` under `update_lock`. |
| **MCP Server & Receipts** | `mcp_gate.py` / `remote_bridge.py` | Stdio JSON-RPC MCP server exposing knowledge and `remote_dev` tools (`mcp_catalog.json`), backed by SQLite WAL `RemoteReceipts`. |
| | `receipt_layout.py` | Resolves `<config>.remote-receipts.sqlite3` and cleans up legacy sidecar artifacts. |
| | `runtime_call.py` | Executes one authorized tool call inside the configured runtime without silent request replay. |
| **Diagnostics & Transcript** | `agent_diagnostics.py` | Stdlib-only diagnostic event capture (`<config>.diagnostics/pending.sqlite3` with WAL + `busy_timeout=5000`). |
| | `diagnostic_support.py` / `diagnostic_ops.py` / `diagnostic_Consumer.py` | Redacts paths/secrets, attaches diagnostic references, and drains pending SQLite events into the shared diagnostics store. |
| | `codex_transcript.py` | Extracts redaction-ready transcript material (`identify`, `read_material`, `capture_source`, `history_source`) from Codex rollout files. |
| **Configuration & Updates** | `setup.py` / `sharing.py` / `summary_provider.py` | Manages MindIE-owned adapter/engine configs, `mindie-community-config/1` consent state, and LLM summary provider credentials. |
| | `update_lock.py` / `update_trigger.py` / `candidate_activate.py` / `candidate_validate.py` | Reentrant file locking (`flock`/`LockFileEx`), background MCP update checks, and transactional candidate validation/activation. |
| **Subprocess & Service** | `bounded_process.py` / `windows_process.py` | Bounded subprocess execution with POSIX process-group and Windows Job Object (`CREATE_SUSPENDED`) tree termination. |
| | `service_client.py` / `service_control.py` / `session_maintenance.py` / `feed_sync.py` | Cold-start service connection, offline `status` / `shutdown`, and session maintenance. |

### Call Graph

```text
Codex Host Hook (UserPromptSubmit / Stop) ──► native_entry inline command
  └─► bootstrap.py ──► entry.py (enter / stop / status / leave)
        ├─► session_gate.py ──► admission_ops.py (Admission SQLite store)
        └─► bridge.py ──► service_client.py / codex_transcript.py / agent_diagnostics.py

Codex Host MCP Client (stdio JSON-RPC) ──► native_entry inline command
  └─► bootstrap.py ──► mcp_gate.py
        ├─► RemoteReceipts (<config>.remote-receipts.sqlite3, WAL + busy_timeout=5000)
        ├─► runtime_call.py (knowledge tools)
        └─► remote_dev.mcp.server (remote-dev tools)
```

---

## 2. Host Lifecycle Contracts & Explicit `/mindie-agent` Gating

1. **Passive Default (No Implicit Hijacking)**:
   - Mere plugin installation or MCP `tools/list` discovery **never** activates a task lease, starts the background knowledge service, or captures transcripts.
   - Ordinary prompts without `/mindie-agent` pass through `UserPromptSubmit` (`entry.py`) untouched (`{"continue": true}`).
2. **Explicit `/mindie-agent` Entry (`UserPromptSubmit`)**:
   - When a prompt invokes `/mindie-agent`, `entry.py` claims/refreshes the session lease via `session_gate.py` and runs `session_maintenance.py`.
   - If the lease is claimed but background service binding fails, `entry.py` returns `status="degraded"` (never rolling back the active lease or failing the hook).
3. **Turn-End Knowledge Capture (`Stop`)**:
   - `entry.py` / `bridge.py` checks whether the current session holds an active lease and community sharing is enabled.
   - If capture fails on an active session, `bridge.py` records a diagnostic reference and returns `status="degraded"` (keeping the session lease active).
4. **MCP Tool Gating & Receipt Accounting (`mcp_gate.py`)**:
   - Read-only / remote tools and knowledge tools validate lease admission as configured in `mcp_catalog.json`.
   - **Receipt Honesty Invariant**: Once a remote tool execution succeeds (`done=True`), if local SQLite receipt persistence (`finish()`) raises an `OSError`/`sqlite3.Error`, `mcp_gate.py` attaches `receipt_warning` + `diagnostic_ref` to `structuredContent` and **never** flips `isError=True` on the succeeded remote result.

---

## 3. Timeout Budgets & `v0.0.2` Anti-Over-Defensive Iron Rules

- **Timeout Hierarchy (`outer_timeout >= sum(inner_budgets) + cleanup_grace`)**:
  - `bounded_process.run` enforces explicit wall-clock deadlines and kills the entire child process group / Windows Job Object on timeout before draining pipes.
  - `STOP_DRAIN (15.0s) >= STOP_GRACE (12.0s) + 3.0s` in `entry.py` / `bridge.py`.
  - Cold-start runtime preparation (`BOOTSTRAP_TOTAL = 540.0s`) runs only when explicitly triggered and uses stage markers (`bootstrap.preparing`).
- **Four `v0.0.2` Iron Rules**:
  1. **Lightweight SQLite Connections**: `agent_diagnostics.py` and `mcp_gate.py` (`RemoteReceipts`) open SQLite with `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`, `PRAGMA busy_timeout=5000`, and `CREATE TABLE IF NOT EXISTS`. Never run `.owner` / `.authority.json` sidecar `lstat` ceremonies or `PRAGMA table_info` / `sqlite_master` schema reflection on every operation.
  2. **Honest External Receipts**: Reserve `unknown` receipts for uncertain remote execution; never turn a succeeded remote call into `isError=True` over a local receipt write error, and never brick local state into permanent `fault`.
  3. **Simple Regular-File I/O**: Use `Path.is_file()` and `Path.open("rb")` for local configuration and transcript reads; do not use `O_NOFOLLOW`/`O_NONBLOCK`/`fstat` inode-recheck paranoia or parent-directory symlink walks (`_present`).
  4. **No Inline String-Blob Runtime Clones**: Keep runtime modules as clean standalone `.py` scripts tested directly via behavior, not byte-for-byte file copy assertions.

---

## 4. Verification & Live NPU Acceptance Commands

Use the shared test virtual environment (`/Volumes/data/code/mindie-release-20261014/codex-interrupt-final-env-20261016/bin/python`) to run the test suite:

```bash
# 1. Full unit, contract, and E2E MCP knowledge suite (Tier 0 + Tier 1)
/Volumes/data/code/mindie-release-20261014/codex-interrupt-final-env-20261016/bin/python -m pytest tests -q --ignore=tests/test_live_npu_acceptance.py

# 2. End-to-end MCP + Knowledge lifecycle verification
/Volumes/data/code/mindie-release-20261014/codex-interrupt-final-env-20261016/bin/python -m pytest tests/test_mcp_knowledge_e2e.py -q

# 3. Live Ascend NPU acceptance on standing A2 (173.125.1.2) & A3 (192.168.13.165) clusters (Tier 2)
/Volumes/data/code/mindie-release-20261014/codex-interrupt-final-env-20261016/bin/python -m pytest tests/test_live_npu_acceptance.py -q
```
