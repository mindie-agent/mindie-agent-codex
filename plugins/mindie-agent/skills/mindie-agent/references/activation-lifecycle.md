# Activation lifecycle

Read this when activation state, capture, quotas or hook behavior matter for
the task at hand.

- The lease lasts at most 24 hours for this session/configuration. Three
  consecutive failed MCP calls pause this session. Expired or paused
  activation requires another explicit user invocation; it is never renewed
  automatically.
- Deactivation prevents new calls and captures; already admitted work remains
  subject to its existing deadline and call budget.
- If `capture` comes back `unbound:<reason>`, continue the task normally; the
  Stop capture is skipped. Do not retry the bind in the background — a later
  explicit re-invocation binds again through the same bounded attempt.
- MCP calls carry no identity arguments. The host binds each tools/call to
  its native task through turn metadata; a call from another task, or from an
  older host without that metadata, fails closed. The remote tools' existing
  `session_id` field is a remote job ID; it must not be replaced with the
  Codex session ID.
- Knowledge MCP calls have a 15-second outer deadline; remote calls have 65
  seconds. Each business call has one attempt and zero automatic retries. A
  timed-out remote mutation may already have executed: inspect its original
  job only if the user asks.
- Background model work is bounded by durable call quotas and a failure
  circuit breaker. A failed or interrupted maintenance item is never replayed
  automatically, including after a restart. Never repair missing hooks by
  restoring retired entrypoints or deleting caches still used by active tasks.
- The Stop hook acts only when community sharing is enabled for the lease's
  authorized project scope. Otherwise it is a no-op: no transcript read, no
  draft, no worker, no model. When enabled it forwards the transcript location
  and a bounded final summary to the local background organizer; it never
  parses transcripts itself and never reads other conversations or hidden
  reasoning. Unavailable capture is dropped. Inactive or recursive Stop events
  are dropped; each session/turn is attempted once, including failed delivery.
- Disabling sharing cancels pending capture and organization in its scope
  without deleting drafts; re-enabling processes only newly authorized
  material and never backfills the disabled period. Toggling sharing never
  invalidates ordinary activation or read tools.
