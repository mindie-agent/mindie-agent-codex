# MindIE Agent — Codex Native Harness Plugin (`mindie-agent-codex`)

This repository implements the **Codex Native Harness Plugin** (`plugins/mindie-agent/`) for MindIE Agent. It bridges the Codex CLI host lifecycle (Hooks, Skills, and stdio MCP server) to the shared runtime components (`mindie_knowledge`, `remote_dev`, `mindie_diagnostics`, `npu_top`, and `coordinator`) without duplicating core domain engines.

---

## 1. Plugin Script Navigation & Call Graph

All plugin scripts live under `plugins/mindie-agent/scripts/` and include standardized top-of-file Agent Contract docstrings (`[Role]`, `[Invariants]`, `[Timeouts & Locks]`, `[Side-Effect Policy]`):

| Layer | Script | Role & Key Responsibilities |
| :--- | :--- | :--- |
| **Native Entry & Bootstrap** | `native_entry.py` | Renders `hooks/hooks.json` and `.mcp.json` commands using an inline Python launcher that delegates to `bootstrap.py`. |
| | `bootstrap.py` | Resolves/prepares the isolated Python runtime (`bootstrap_runtime.py`) and dispatches `entry`, `mcp-gate`, `remote-bridge`, or `bridge`. |
| **Host Hook Lifecycle** | `entry.py` | Native hook & CLI entry controller (`session-start`, `user-prompt-submit`, `stop`, `bridge`). Ordinary prompts on `user-prompt-submit` return `{}` immediately before resolving the profile. |
| | `manual_entry.py` | Handles explicit `/mindie-agent` and `$mindie-agent` (`$mindie-agent:mindie-agent`) gestures on `UserPromptSubmit` for main sessions (`not event.get("agent_id")`), granting `Admission.activate` (`entry_enabled=1`) or revoking registered domain leases on `/mindie-agent deactivate`. |
| | `bridge.py` | CLI & Hook dispatcher for `activate` (requires prior `UserPromptSubmit` `knowledge_lease`), `deactivate`, `status` (factual control-plane status without onboarding prompts), `config`, `history-import`, `recover`, `shutdown`, and `stop` capture. Returns `status="degraded"` when capture fails so active leases stay intact. |
| | `session_gate.py` / `admission_ops.py` | Entry-bound task lease management backed by `mindie_knowledge.loop.activation.Admission` (`knowledge_lease` / `entry_enabled=1`) under `update_lock`. |
| **MCP Server & Receipts** | `mcp_gate.py` / `remote_bridge.py` | Stdio JSON-RPC MCP server exposing knowledge and `remote_dev` tools (`mcp_catalog.json`), backed by SQLite WAL `RemoteReceipts`. |
| | `receipt_layout.py` | Resolves `<config>.remote-receipts.sqlite3` and cleans up legacy sidecar artifacts. |
| | `runtime_call.py` | Verifies `Admission.knowledge_lease(session_id)` (`entry_enabled=1`, raising `KnowledgeInactive` when unactivated) and executes one authorized knowledge tool call inside the configured runtime without silent request replay. |
| **Diagnostics & Transcript** | `agent_diagnostics.py` | Stdlib-only diagnostic event capture (`<config>.diagnostics/pending.sqlite3` with WAL + `busy_timeout=5000`). |
| | `diagnostic_support.py` / `diagnostic_ops.py` / `diagnostic_Consumer.py` | Redacts paths/secrets, attaches diagnostic references, and drains pending SQLite events into the shared diagnostics store. |
| | `codex_transcript.py` | Extracts redaction-ready transcript material (`identify`, `read_material`, `capture_source`, `history_source`) from Codex rollout files. |
| **Configuration & Updates** | `setup.py` / `sharing.py` / `summary_provider.py` | Manages MindIE-owned adapter/engine configs, `mindie-community-config/1` consent state, and LLM summary provider credentials. |
| | `update_lock.py` / `update_trigger.py` / `candidate_activate.py` / `candidate_validate.py` | Reentrant file locking (`flock`/`LockFileEx`), background MCP update checks, and transactional candidate validation/activation. |
| **Subprocess & Service** | `bounded_process.py` / `windows_process.py` | Bounded subprocess execution with POSIX process-group and Windows Job Object (`CREATE_SUSPENDED`) tree termination. |
| | `service_client.py` / `service_control.py` / `session_maintenance.py` / `feed_sync.py` | Cold-start service connection, offline `status` / `shutdown`, and session maintenance. |

### Call Graph

```text
Codex Host Hook (SessionStart / UserPromptSubmit / Stop) ──► native_entry inline command
  └─► bootstrap.py ──► entry.py (session-start / user-prompt-submit / stop / bridge)
        ├─► manual_entry.py (UserPromptSubmit: /mindie-agent | $mindie-agent ──► Admission.activate)
        ├─► session_maintenance.py (SessionStart: diagnostic_maintenance; skips service/feed when inactive)
        └─► bridge.py ──► admission_ops.py / service_client.py / codex_transcript.py / agent_diagnostics.py

Codex Host MCP Client (stdio JSON-RPC) ──► native_entry inline command
  └─► bootstrap.py ──► mcp_gate.py
        ├─► RemoteReceipts (<config>.remote-receipts.sqlite3, WAL + busy_timeout=5000)
        ├─► runtime_call.py (knowledge tools; requires Admission.knowledge_lease)
        └─► remote_dev.mcp.server (remote-dev tools; available without /mindie-agent)
```

---

## 2. Host Lifecycle Contracts & Explicit `/mindie-agent` Gating

1. **Passive Default (No Implicit Hijacking)**:
   - Mere plugin installation, `SessionStart`, saved contribution consent, or MCP `tools/list` discovery **never** activates a knowledge lease (`entry_enabled=1`), starts the background knowledge service, syncs feeds, or captures transcripts.
   - Ordinary prompts without `/mindie-agent` or `$mindie-agent` short-circuit immediately on `UserPromptSubmit` (`entry.py` emits `{}` before resolving the profile).
   - `SessionStart` (`session_maintenance.py`) runs `diagnostic_maintenance` and returns `{"status": "inactive", "diagnostics": ...}` whenever `Admission.knowledge_lease(CODEX_THREAD_ID)` is `None`.
2. **Explicit `/mindie-agent` Entry (`UserPromptSubmit` + `bridge activate`)**:
   - When a main-session prompt (`not event.get("agent_id")`) begins with `/mindie-agent` or `$mindie-agent` (`$mindie-agent:mindie-agent`), `manual_entry.py` calls `Admission.activate` (`entry_enabled=1`) and `entry.py` invokes `session_start(config, session_id=...)` to prepare the session (preserving the committed grant with `systemMessage` if preparation fails), returning `hookSpecificOutput` instructing the Agent to follow `SKILL.md` and run `entry.py bridge activate` once.
   - `bridge.py activate` and `history_import.py` (`bridge history-import`) verify `Admission.knowledge_lease(native_session())` first (rejecting unactivated calls without auto-associating); `/mindie-agent deactivate` revokes the session's leases across registered domains via `deactivate_registered_domains`.
3. **Turn-End Knowledge Capture (`Stop`)**:
   - `bridge.py` (`stop`) and `admission_ops.py` (`stop_capture`) check `Admission.knowledge_lease(session)` (`entry_enabled=1`) before reading `sharing` settings or inspecting transcripts, and verify `lease["project_root"] == source["project_root"]` without auto-associating unactivated sessions.
   - If capture fails on an active session, `bridge.py` records a diagnostic reference and returns `status="degraded"` (keeping the session lease active).
4. **MCP Tool Gating & Receipt Accounting (`mcp_gate.py`)**:
   - Deterministic `remote-dev` tools and factual `bridge.py status` checks remain available in ordinary sessions without `/mindie-agent`.
   - All `knowledge_*` MCP tools (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) check `Admission.knowledge_lease(session_id)` in `runtime_call.py` and fail with `KnowledgeInactive` (`code="knowledge_inactive"`) unless the current session was manually enabled via `/mindie-agent` or `$mindie-agent`.
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
