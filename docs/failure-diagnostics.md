# Failure Diagnostics & Cancellation Boundaries

<a href="failure-diagnostics.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

Following Principle 9 of the [MindIE Agent Engineering Design Principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.en.md) (*observable, diagnosable tools; never hide errors*), `mindie-agent-codex` provides read-only status inspection, deterministic local process cleanup on cancellation, and clear separation from remote Ascend background jobs.

---

## 1. Read-Only Status Inspection (`bridge status`)

- **Clear State Distinction**: `bridge.py status` distinguishes between a missing configuration, a corrupted/invalid configuration file, an occupied update lock, and a failed runtime. Corrupted settings are reported directly as a diagnostic error and never trigger a fresh install/onboarding prompt.
- **Task-Scoped Snapshot**: Using the native `CODEX_THREAD_ID`, `status` returns factual control-plane state (`experience`, `sharing`, `admission` lease state, startup stage, configuration/storage/service health, `diagnostics.reporting` status, `updates` check state, and the latest 5 captures, summaries, and contribution batches for that task; or `configured: false` when unconfigured) without injecting onboarding prompt fields (`first_use`, `next`, `recovery`). When called outside a task identity, it returns no task records.
- **Strictly Read-Only**: Running `status` (within its 5-second execution budget) never binds a task, resets failure counters, starts background services, or replays failed work. Uncertain external PR publications must be reconciled before any explicit `recover` retry.

---

## 2. Local Cancellation vs. Durable Remote Jobs

- **Sanitized Remote Exceptions**: Remote error responses return fixed safe error categories, submission certainty, and the existing remote job reference—omitting raw provider stderr or arbitrary untrusted text.
- **Local Process Cleanup vs. Remote Lifecycle**: Cancelling a local tool call or closing stdin (`EOF`) cleans up the local helper and SSH subprocess tree owned by that invocation. However, cancelling a local observation call does **not** prove a durable remote compilation or test job on the Ascend server has stopped; use `remote-dev` job controls to inspect or terminate the remote workload explicitly.
- **Independent Remote Execution**: Even if the local knowledge service fails, native shell/SSH and `mindie-remote-dev` remain available.


## Release activation evidence

Activation failures now transport a bounded structured envelope from the owned
activation child before its traceback is lost at the subprocess boundary. Local
failure events and authorized issue evidence retain the child package identity,
owned module/function/line, exception class chain and typed errno. The child records
once, and new parents reuse its incident reference. Older parents that cannot
decode the richer envelope still retain the child event. Unknown child
crashes remain internal failures; expected catalog, prerequisite and connectivity
failures keep their existing classifications. An incomplete zero-exit activation
also records diagnostics while retaining its incomplete result and binding facts.

Shared `mindie-diagnostics` validates every transported field. Raw messages,
stdout/stderr, commands, configuration, local paths and user-code frames are not
published. Consent and incident deduplication remain unchanged. Legacy children
without this envelope remain supported, but their missing evidence cannot be
reconstructed. This improves future reports related to mindie-agent#324; it does
not identify the original 0.1.20 incident's root cause or resolve that incident.
