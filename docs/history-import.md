# Explicit historical contribution

Historical import is a manual operation for a user who explicitly wants to
contribute their own past experience. `/mindie-agent`, enabling community sharing,
Stop events, update checks and knowledge synchronization never call it. The
Skill interprets the user's request; the runtime does not pretend to infer human
intent from a boolean or the words inside a transcript.

The importing native session must have invoked `/mindie-agent`. The existing
installation-level contribution choice and project scope are reused without
new onboarding. This operation makes a narrow exception for user-selected past
transcripts: the historical sessions themselves need not have been activated.
They remain inactive, and no automatic capture is enabled for them.

```sh
python3 /absolute/active/plugin/scripts/bridge.py history-import \
  --source /absolute/selected/rollout.jsonl \
  --source /absolute/another/rollout.jsonl
```

Use `python` on Windows. Source paths must be absolute regular files. There is
no default source, directory scan, MCP import tool, scheduled import, or watcher.
Locate files through native tools only after an explicit request covering those
sessions; clarify an ambiguous selection. Native `session_meta` supplies the
source session identity and `cwd`. Missing metadata or an out-of-scope project is
reported instead of guessing it from the importing task. The parser still excludes
inherited fork material, tools, hidden reasoning and injected instructions.

Each source is processed sequentially to its initially observed byte boundary.
Later appends wait for another explicit import. Public messages are assembled and
redacted as one projection, so a multiline private-key fragment cannot evade the
scanner at a page boundary. No raw planning packets or transcript copies are
retained. Working memory is proportional to one transcript's public projection,
not the number of selected files; parser page targets are not a hard peak-memory
cap. The existing canonical-entry platform envelope applies, with a visible
failure rather than silent clipping. Corrupt skipped records are counted in the
receipt; incomplete tails, replacement, unsupported formats or scanner failure
do not save a partial entry for that file. Earlier successfully imported files
remain saved if a later file fails or the command is interrupted.

One latest private receipt per source identity stores only entry/ref, public
content length/hash and sharing generation. Identical public material is a no-op,
even from a relocated copy of the same session. A subsequent explicit import of
an appended source adds only new public material to its original entry. A changed
old prefix or changed contribution generation is reported, not used to overwrite
existing or remotely corrected content. A compacted submitted entry is restored
through the existing confirmed-PR/main path before adding new material. This
deduplicates explicit imports; it does not semantically merge unrelated sessions
or an independently created ordinary-capture entry.

The body uses the same deterministic public-transcript contract as current
capture. A labeled excerpt supplies the retrieval title/summary, without a model
call or a claim of semantic synthesis. The existing outbox performs final checks,
PR submission and normal review/merge. JSON lines report each file's `imported`,
`extended`, `unchanged`, `empty`, or `failed` result. The final service receipt means
delivery is prepared, not publication or merge confirmed. Service startup failure
retains the saved draft; an explicitly repeated import deduplicates it and can
prepare delivery again. Consent is rechecked before every source/page and commit;
outbound authorization remains enforced by the existing contribution worker.
