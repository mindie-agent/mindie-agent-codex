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
message is processed whole. Invalid input cannot be represented as complete coverage. No semantic shortening or model call belongs to this step.
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

Setup and updates automatically install the required title/summary worker.
The adapter owns its fixed GPT-6-Luna / low policy; there is no user-facing
model or effort setting and no inheritance from the business task. A separate
worker reads the complete redacted body. It can only update title and summary;
it cannot rewrite the body or hold up later captures. The native invocation
deadline is 35 seconds, with a 45-second outer cancellation deadline. A failed
or superseded attempt keeps the body locally but does not qualify it for a new
export batch. Missing worker configuration is a visible error, not a normal
excerpt mode. Local saving, summary completion and publication are separate
states. Current worker input is still the full body; token-aware incremental
organization for long material is not implemented.
Updates replace obsolete model arguments with the installed adapter policy.

CI validates actual stored/exported content, atomic rollback, duplicate/restart
behavior, summary failure and unauthorized fields, real Gitleaks positives and
technical negatives, public projection and 1 MiB/10 MiB messages, cold service
startup and native Windows console ownership. Native business acceptance also
checks real Stop delivery, installed immutable runtime, actual remote PR bytes
and retrieval. Component doubles do not prove those external boundaries.
