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
- The remote tools' existing `session_id` field is a remote job ID; it must
  not be replaced with the Codex session ID.
- Knowledge MCP calls have a 15-second outer deadline; remote calls have 65
  seconds. Each business call has one attempt and zero automatic retries. A
  timed-out remote mutation may already have executed: inspect its original
  job only if the user asks.
- Background model work is bounded by durable call quotas and a failure
  circuit breaker. A failed or interrupted maintenance item is never replayed
  automatically, including after a restart. Never repair missing hooks by
  restoring retired entrypoints or deleting caches still used by active tasks.
- The Stop hook submits the final response to the local background organizer.
  It does not read other conversations or hidden reasoning. Unavailable
  capture is dropped. Inactive or recursive Stop events are dropped; each
  session/turn is attempted once, including failed delivery.
