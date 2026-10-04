# Public transcript capture

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
Each invocation reads complete new blocks and short prior task navigation, then
returns one index per block and updated navigation. Blocks are at most 16 KiB;
the serialized user prompt is at most 64 KiB with at most eight blocks. Native
system/context tokens are additional and are counted in reported usage. The
native invocation deadline is 90 seconds with a 100-second outer deadline.
There is no final full-transcript merge or repeated old-body input.

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
