# Public transcript capture

[中文](public-transcript.zh.md) | English · [Documentation Index](README.en.md)

The Codex reader retains native `response_item` public user/assistant text,
including attributed commentary and final messages. It excludes tools, hidden
reasoning, system/developer messages, injected catalogs, duplicate wrappers and
inherited fork history. Equal messages at different positions remain distinct.
Missing timestamps, corrupt records and incomplete coverage fail visibly rather
than advancing a cursor. Attachment placeholders never fetch their referenced
files or URLs.

Gitleaks 8.30.1 and the existing privacy rules run locally. Setup/update install
the fixed scanner release with its checked hash and license. Ambient scanner
configuration and source `gitleaks:allow` comments cannot disable checks. The
incremental scanner carries only open private-key state across pages; secret
text is never buffered in that state. Stable HMAC replacements preserve equality
for relevant privacy fields, and credentials use a canonical redaction marker.
Mechanical rules do not establish that proprietary prose is public; the approved
project scope remains the contribution boundary.

The core stores complete redacted material as stable Markdown blocks and commits
its cursor, admission, manifest binding and index queue together. SQLite holds
small state and receipts, not another body library. Duplicate native delivery
cannot append the same range twice. Explicit historical import uses this same
pipeline. Current published blocks remain the continuation base; withdrawn
material is not resurrected from a stale PR.

Setup and updates install the LangMem summary worker. Its explicit policy is
`gpt-5.6-luna` / `low`, using the native account without a second provider setup.
Setup and updates retain the selected native home and executable as installation
paths. The MCP, Stop, service and worker entries inherit these paths even when
the native host filters its environment. The ordinary default is `~/.codex`.
When that native home selects a custom provider, the worker projects only
that provider's routing and authentication fields into a private temporary
native home. It does not copy native login files, tools, skills, hooks or rules.
A supplied model catalog must contain exactly one matching fixed-model name or
qualified alias. Without a catalog the provider must accept the exact fixed
name; no prefix is inferred from the business model. A provider rejection is a
visible failed attempt, with its actual usage retained and no automatic retry.
The policy identity records the effective model and non-secret route identity.
Rotating a declared bearer/environment credential or a recognized credential
query/header value does not trigger reindexing. Other query/header values
contribute only to the route hash; an unknown credential parameter may therefore
change policy when rotated. Provider query parameters and headers are passed
through unchanged.
Unsupported provider fields, missing referenced authentication environment
variables and top-level native `profile` selection fail before spawning.
The selected provider must be declared directly in that installation's
`config.toml`; the worker does not merge native profiles.
Custom providers requiring native OpenAI account authentication are rejected
before a model starts: that login state is not copied into the temporary home.
Built-in OpenAI authentication
retains its existing native home.
Each invocation reads complete new blocks and short prior task navigation, then
returns one index per block and updated navigation. Blocks are at most 16 KiB;
the serialized user prompt is at most 64 KiB with at most eight blocks. Native
system/context tokens are additional and are counted in reported usage. Native
execution and worker identity checks have no default wall-clock deadline. The
owner can cancel them on service shutdown or revoked authority. Terminal output
is the completed visible `agent_message` from the native JSONL stream, paired
with `turn.completed` and its usage. Provisional items, commentary and hidden
reasoning are not results. Indexing does not depend on a CLI output file written
during shutdown. The core saves the result before closing its owned process
tree; a cleanup failure cannot erase it or authorize another paid call. There
is no final full-transcript merge or repeated old-body input.

The outcome ledger distinguishes returned, failed and unknown invocations and
retains nullable native token usage. Returned outputs survive local scanner or
apply failures without another model call. Failed/unknown calls are not repeated
automatically. Missing worker configuration and incomplete indexes block export.
Local saving, completed indexing, GitHub publication and cleanup have distinct
receipts; none is inferred from process liveness.

The current public format is a task manifest plus ordered blocks. Consumers
reuse the producer's fallible indexes and rebuild local ReMe retrieval without
model calls. Superseded fixed references expire; current Markdown files remain
the body authority. Version 3 local databases are not read or migrated.

Component checks cover actual parser/scanner behavior, atomic commits, duplicate
and restart handling, index/outcome failures and bounded process ownership.
Anonymous evidence from four selected real K3 histories is committed in the core
repository. Native Stop delivery, immutable installed runtime, actual GitHub PR
bytes and an independent public consumer are separate acceptance boundaries.
