# Explicit Historical Session Import

<a href="history-import.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

The `history-import` command lets you explicitly import selected historical Codex JSONL transcripts into the community contribution pipeline. Normal daily usage, `Stop` hooks, update checks, and feed syncs **never** scan or import historical transcripts automatically.

---

## 1. Command Usage & Authorization Checks

`v0.1.38` no longer exposes a `bridge.py` command. Historical import is not a thin-plugin entrypoint; when the user explicitly requests it, call the shared `mindie_knowledge.loop.history_import` API with one or more selected JSONL paths and the current task's verified lease. No automatic discovery or directory scan is performed.

- **No Automatic Discovery**: Only the exact files passed via `--source` are read; there is no directory watcher or background history scanner.
- **Requires Active Task Lease & Approved Scope**: The requesting session (`CODEX_THREAD_ID`) must hold an active task lease (`authority.knowledge_lease(session)`)—Historical import never auto-associates an unactivated session. Both the requesting session and the historical transcript's working directory must fall within your already approved contribution project scope (`sharing.capture_allowed`). No duplicate consent prompt is shown, and historical sessions remain inactive.

---

## 2. Incremental Redaction & Resumable Intake

- **Shared Redaction Pipeline**: Historical files are read in complete pages up to their initial byte length and processed through the exact same public-message filter, Gitleaks `8.30.1` scanner, and Markdown block writer used by live `Stop` capture.
- **Safe Partial Progress**: A durable intake marker prevents publishing a partial snapshot until every page of the selected file is admitted. If a later page fails, earlier pages remain safely stored locally so you can resume without re-running completed work.
- **Deduplicated Repeat Runs**: Re-running the import on the same file verifies the already-consumed prefix and imports only newly appended turns, without creating duplicate blocks or making extra model calls.

---

## 3. Indexing Receipts & Explicit Retry

- **Clear Stage Separation**: The JSON receipt reports local intake status (`imported`, `extended`, `unchanged`, `empty`, or `failed`), indexing status, and pending publication separately.
- **Explicit Summary Retry (`--retry-summary`)**: If an indexing model call fails or returns an uncertain result, it is never retried automatically in the background. To explicitly retry indexing for a selected file after inspecting the status, call the shared API with its retry flag (all prior token usage remains recorded in the ledger).
