# Failure Diagnostics & Cancellation Boundaries

<a href="failure-diagnostics.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

Following Principle 9 of the [MindIE Agent Engineering Design Principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.en.md) (*observable, diagnosable tools; never hide errors*), `mindie-agent-codex` provides read-only status inspection, deterministic local process cleanup on cancellation, and clear separation from remote Ascend background jobs.

---

## 1. Read-Only Status Inspection (`bridge status`)

- **Clear State Distinction**: `bridge.py status` distinguishes between a missing configuration, a corrupted/invalid configuration file, an occupied update lock, and a failed runtime. Corrupted settings are reported directly as a diagnostic error and never trigger a fresh install/onboarding prompt.
- **Task-Scoped Snapshot**: Using the native `CODEX_THREAD_ID`, `status` returns the startup stage, configuration/storage/service health, active task binding, diagnostic failure counter, and the latest 5 captures and contribution batches for that task (returning no task records when called outside a task identity).
- **Strictly Read-Only**: Running `status` (within its 5-second execution budget) never binds a task, resets failure counters, starts background services, or replays failed work. Uncertain external PR publications must be reconciled before any explicit `recover` retry.

---

## 2. Local Cancellation vs. Durable Remote Jobs

- **Sanitized Remote Exceptions**: Remote error responses return fixed safe error categories, submission certainty, and the existing remote job reference—omitting raw provider stderr or arbitrary untrusted text.
- **Local Process Cleanup vs. Remote Lifecycle**: Cancelling a local tool call or closing stdin (`EOF`) cleans up the local helper and SSH subprocess tree owned by that invocation. However, cancelling a local observation call does **not** prove a durable remote compilation or test job on the Ascend server has stopped; use `remote-dev` job controls to inspect or terminate the remote workload explicitly.
- **Independent Remote Execution**: Even if the local knowledge service fails, native shell/SSH and `mindie-remote-dev` remain available.
