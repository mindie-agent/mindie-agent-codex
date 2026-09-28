# Activation lifecycle

Read this when binding state, capture, or hook behavior matter for the
task at hand.

- The per-task lease is the internal binding the entry establishes
  automatically. It lasts for this native task until the user explicitly
  unbinds it (`deactivate`) or the project scope actually changes. There is no
  wall-clock expiry, no failure-count pause and no runtime/config-byte
  fingerprint: changing sharing or knowledge config bytes does not revoke a
  binding, and ordinary failures never require deactivate/reactivate. A
  rejected read argument or reference is a correctable input error, not a
  runtime failure. The consecutive-failure counter is diagnostic only.
- Unbinding prevents new knowledge calls and captures; already admitted work
  remains subject to its existing deadline. Attempt identities are preserved so
  revoke/rebind never replays old captures. A fresh binding rotates the
  token and capture boundary.
- First explicit invocation runs `bridge.py activate` once: it binds the task
  and returns status. If sharing is unconfigured, obtain only missing destination and scope;
  do not offer partial product modes or claim capture is ready. The saved choice
  persists for the installation — new tasks, forks, restarts, upgrades and
  failures never re-ask and never revoke it; an explicit off stays off.
  Configure sharing with `setup.py configure` or `bridge.py config`; do not
  reinstall or hand-edit JSON.
- If `capture` comes back `unbound:<reason>`, continue the task normally; the
  Stop capture is skipped. Do not retry the bind in the background — a later
  explicit re-invocation binds again through the same bounded attempt.
- MCP calls carry no identity arguments. The host binds each tools/call to
  its native task through turn metadata; a call from another task, or from an
  older host without that metadata, fails closed. Native identity for
  binding is `CODEX_THREAD_ID` only. The remote tools' existing `session_id`
  field is a remote job ID; it must not be replaced with the Codex session ID.
- Knowledge MCP calls have a 15-second outer deadline; remote calls have 65
  seconds. Each business call has one attempt and zero automatic retries. A
  timed-out remote mutation may already have executed: inspect the original
  job or receipt as needed to finish the authorized task. A status read does
  not repeat the mutation; do not blindly resubmit it.
- Background model work is bounded by durable call quotas. A region that
  failed with an explicit deadline gets exactly one delayed background
  recovery (at most two model attempts in total, persisted across restarts);
  a second failure keeps a locatable, non-complete gap while other captures
  continue. Saved results are never re-applied and succeeded regions are
  never re-organized. Never repair missing hooks by restoring retired
  entrypoints or deleting caches still used by active tasks.
- The Stop hook acts only when community sharing is enabled for the lease's
  authorized project scope. Otherwise it is a no-op: no transcript read, no
  capture row, no worker, no model. When enabled it commits the native
  session, turn, and transcript location through the shared handoff. An
  optional over-long final summary is dropped when a transcript reference is
  present. Success is a durable capture stage, not a claim that the model ran.
  A refused connection does not affect the binding. The same session and turn
  commit once. Inactive or recursive Stop events are ignored.
- Disabling sharing cancels pending capture and organization in its scope
  without deleting drafts; re-enabling processes only newly authorized
  material and never backfills the disabled period. Toggling sharing never
  invalidates the task binding or read tools.
- Contribution inspect, reconcile, retry, and compact are optional troubleshooting for one existing batch, not ordinary recovery. A transient local or network failure is handled by the existing worker, and sharing status is how a problem is seen. Authentication, trust, rejected content, or invalid configuration can need an explicit user or operator action. These commands do not rerun the organizer, reset a capture cursor, or replay a model. Uncertain writes are inspected or reconciled, never blindly retried.

Remote tools work in every native task without a binding. Their 65-second
call deadline, durable no-replay receipts and automatic failure backoff are
independent of knowledge. Job records are isolated by native task. Repeated
helper failures back off with a growing persisted delay and resume
automatically; `remote_bridge.py recover` from that task is only an optional
early release and never replays earlier calls. No background retry or model
turn performs recovery.

Idle task bindings do not block plugin updates. Updates wait for actual
in-flight calls and in-flight maintenance/publication, then switch one
committed generation (interpreter + scripts + transcript parser). Existing
remote jobs keep their identity. Old cached native entrypoints stay callable.
