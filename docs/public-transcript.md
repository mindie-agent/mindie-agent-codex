# Public transcript capture

Codex contributes a locally redacted conversation, not a model-authored case
report. This is an explicit `capture_mode: public-transcript` in the selected
engine configuration. Other adapters keep their existing behavior until they
have their own parser and product acceptance.

The Codex reader retains only native `response_item` public user/assistant text.
Assistant commentary and final-answer phases remain attributed. It excludes
tools, internal reasoning, system/developer messages, known injected catalogs,
duplicate event wrappers and inherited fork history. Equal public messages at
different positions are retained. Reads page between records; a long public
message is processed whole. Oversized/invalid input cannot be represented as
complete coverage. No semantic shortening or model call belongs to this step.
Missing public-message timestamps hold the cursor because the authorization
boundary cannot be verified. Supported attachment types leave a safe placeholder;
their URLs, files and binary data are never read. Both native text shapes use
the same injection/citation filters, applied per content part.

Gitleaks 8.30.1 supplies secret rules; the existing privacy scanner supplements
paths, addresses and identities. Setup/update download a fixed release with a
committed SHA256 and retain its MIT license. Runtime scans are local and ignore
ambient repository configuration and `gitleaks:allow` comments. Findings and
credentials are never diagnostics. Replacements use stable HMAC placeholders
with a private random key, preserving equality without storing a cleartext map.
Rule scanning cannot establish whether proprietary prose or an algorithm is
public: the already-approved project scope remains the contribution boundary.
Repository review occurs after upload and cannot protect a secret leaked into
an earlier commit.

The existing service commits body, region, cursor and continuation in one SQLite
transaction. If a write fails or the process stops before commit, the cursor
does not advance. Native duplicate delivery cannot append the same bytes twice.
The existing outbox and exact remote receipts own publication. After compaction,
a continuation restores the authoritative body through its receipt; a closed,
unmerged or withdrawn contribution is not silently recreated.

The default metadata is a source excerpt. To enable the optional native summary
worker, explicitly configure the engine's `summary_command` as the selected
runtime Python, selected scripts/agent_worker.py, `--model`, and a model name
that this account supports with reasoning effort `none`. Setup does not infer
this from business configuration. A service restart loads the change; updates
retain explicit summary configuration. No credentials belong in this argv.
The summary subprocess has no tools, hooks, inherited rules or body-writing
field. Only `title` and `summary` are accepted, redacted again and applied if the
body version still matches. New observations coalesce before another attempt.
An interrupted or failed attempt is recorded and the body remains usable.

At validation time, the available ChatGPT Codex account rejected GPT-5.4, and
its advertised GPT-6 models did not offer effort `none`. That is an unavailable
optional summary channel, not a successful non-thinking-model acceptance.

CI validates actual stored/exported content, atomic rollback, duplicate/restart
behavior, summary failure and unauthorized fields, real Gitleaks positives and
technical negatives, public projection and 1 MiB/10 MiB messages, cold service
startup and native Windows console ownership. Native business acceptance also
checks real Stop delivery, installed immutable runtime, actual remote PR bytes
and retrieval. Component doubles do not prove those external boundaries.
