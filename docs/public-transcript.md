# Public Transcript Capture & Local Redaction

<a href="public-transcript.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

When community sharing is explicitly enabled for an approved project scope, `mindie-agent-codex` extracts public conversation messages from native Codex JSONL transcripts, runs local deterministic redaction, stores immutable Markdown blocks, and generates bounded navigation metadata.

---

## 1. Deterministic Public Message Filtering

- **Included Content**: Only public user prompts, visible assistant progress commentary, and final assistant answers (`response_item` records) from the active task.
- **Excluded Content**: Tool calls and outputs, hidden reasoning tokens, system/developer instructions, injected catalogs, duplicate wrappers, and inherited fork history. Attachment placeholders never fetch external files or URLs.
- **Strict Integrity Checks**: Records missing timestamps or containing corrupted JSON fail visibly rather than silently advancing the capture cursor.

---

## 2. Local Redaction with Gitleaks `8.30.1` & Privacy Rules

- **Pinned Local Scanner**: Setup and updates install a verified release of **Gitleaks `8.30.1`** alongside deterministic privacy rules. Ambient user Gitleaks configs and in-text `gitleaks:allow` comments cannot bypass scanning.
- **Page-Streaming Without Buffering Secrets**: Transcripts are scanned in pages; only an open multi-line private-key state flag is carried across page boundaries—raw secret text is never buffered in state.
- **Consistent Redaction Markers**: Credentials are replaced with canonical redaction markers, while sensitive identifiers use stable HMAC replacements so relationships remain readable without exposing raw values.
- **Atomic Block Commits**: Redacted text is saved as immutable Markdown blocks (`<= 16 KiB` per block) in the same SQLite transaction as the read cursor, task manifest binding, and index queue.

---

## 3. Isolated Block Indexing (`gpt-5.6-luna` / `low`)

- **Navigation Metadata Only**: The background index worker invokes the configured native small model (`gpt-5.6-luna` with `low` effort, up to 8 blocks / `64 KiB` prompt per batch) to generate block titles, brief summaries, and task navigation. It **never** rewrites or replaces the redacted Markdown body.
- **Isolated Temporary Home**: When using a custom provider declared in `config.toml`, the worker projects only that provider's routing and authentication settings into an isolated temporary home—never copying user hooks, skills, MCP servers, or unrelated login state.
- **Authoritative Outcome Ledger**: Token usage and results are recorded before subprocess cleanup. Completed, failed, and uncertain indexing calls are tracked distinctly; failed or uncertain paid calls block export until reconciled and are never automatically retried in the background.
