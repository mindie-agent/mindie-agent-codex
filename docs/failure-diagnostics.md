# Failure diagnostics

`bridge.py status` distinguishes missing configuration from invalid configuration,
an occupied update lock, and a failed selected runtime. Existing invalid settings
never become first-use/install prompts. Reads reject FIFOs/devices and oversized
adapter metadata; runtime execution stays under the existing five-second budget.

The selected runtime reads the shared diagnostic snapshot using only native
`CODEX_THREAD_ID`. It returns safe startup stage, configuration/storage/service
state, task admission (including paused), shared maintenance pause, and the latest
five captures/contribution batches associated with the task. Without native task
identity it returns no task records. Recover commands use the selected interpreter
and scripts. Unknown publication writes require inspection/reconciliation before
an explicit retry; status does not activate, reset, start or replay work.

Remote exception responses preserve fixed safe categories, submission certainty
and an existing job reference. They omit arbitrary error text and provider stderr.
Local cancellation does not prove a remote job stopped. Knowledge failure does not
prevent native shell/SSH or independently configured remote-dev work.

Actual macOS validation covered malformed/oversized/FIFO configuration, an external
update lock, failing/malformed/slow helpers, task-isolated SQLite state, failed
parser startup and a delayed loopback service. All owned processes were cleaned
up. A real Luna/max task used one status call to identify broken JSON and propose a
concrete syntax check and native-tool bypass, without retrying or reinstalling.
These claims do not establish new native Hook delivery, full contribution
publication or Windows hardware acceptance. SQLite may create normal WAL-reader
sidecars while leaving business data unchanged.
