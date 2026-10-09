<p align="center">
  <img src="https://raw.githubusercontent.com/mindie-agent/mindie-agent/main/assets/brand/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

# MindIE Agent · Codex

A native Codex plugin for NPU infrastructure work, initially the `vllm-ascend`
domain. It inherits the [nine VAWS / MindIE principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md).
The native task remains in charge; knowledge is optional reference material.

Read the [用户须知 / contribution notice](plugins/mindie-agent/skills/mindie-agent/references/user-notice.md)
for the full visible-message sharing scope, deterministic redaction limits,
model-provider processing and public Git history. It adds no authorization step.

The plugin provides one informational entry Skill, three knowledge tools
(query, block reading and optional feedback), and general remote-dev tools.
It owns Codex identity, transcript parsing, Hook translation and model execution.
The shared knowledge runtime owns retrieval, task admission, contribution
receipts and local cleanup. Claude Code and Kimi have independent adapters.
Old business Skills and profiling remain deferred.

## Install on macOS

Requires Python 3.11+, Git, `uv`, and an authenticated Codex CLI with native
plugin support.

Installation probes the actual pinned runtime, including ReMe, LangMem and the
summary outcome ledger. SQLite stores metadata; retrieval does not require FTS5
or `contentless_delete`.

[product-contract.json](product-contract.json) binds the public content baseline,
its declaration digest and candidate validation protocol. Runtime commit pins
live only in [runtime-requirements.txt](runtime-requirements.txt). The candidate's
own interpreter verifies its installed pins and APIs; the running updater checks
a bounded receipt instead of importing a different runtime's private API.
Content may advance under the same declaration without a plugin release.
Changed declarations require a matching product combination and fail before
installation, publication or feed promotion when they do not match. Cached
candidates recheck local runtime and packaged bytes before installation.

Sign in to Codex, then install from a downloaded copy:

```sh
git clone https://github.com/mindie-agent/mindie-agent-codex.git
cd mindie-agent-codex
MINDIE_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/mindie-agent"
MINDIE_BOOTSTRAP="$MINDIE_DATA/codex-bootstrap/runtime"
uv venv --python 3.11 "$MINDIE_BOOTSTRAP"
uv pip install --python "$MINDIE_BOOTSTRAP/bin/python" -r runtime-requirements.txt
"$MINDIE_BOOTSTRAP/bin/python" plugins/mindie-agent/scripts/setup.py install \
  --knowledge-python "$MINDIE_BOOTSTRAP/bin/python" --root "$MINDIE_DATA"
"$MINDIE_BOOTSTRAP/bin/python" plugins/mindie-agent/scripts/auto_update.py enable \
  --source-root "$PWD" --root "$MINDIE_DATA/updates" --channel main
```

Setup writes MindIE-owned configuration and defaults community contribution to
off. The updater packages the source, installs through the native Codex plugin
API, verifies the selected version and registers the macOS update schedule.
After both steps succeed, the downloaded repository can be moved or removed.
Keep the persistent data directory and bootstrap interpreter: the updater and
retained native entries use them even after a managed runtime is selected.
The updater never resets or stashes a developer checkout.

Review and trust a changed Stop Hook in the native Codex UI when you want it to
run, then start a new task. Hook trust belongs to the user; installation and
updates do not grant it. Use Codex's native permission controls for tools.

Default configuration is `~/.config/mindie-agent/codex.json`; data is under
`~/.local/share/mindie-agent`. For a separate installation, supply `--config`
to setup and `MINDIE_AGENT_CONFIG` to the one-time updater enable command.
The generated Skill, MCP and Hook entries retain that configuration binding;
ordinary later tasks do not need to export it.

## Install on Windows PowerShell

Requires Python 3.11+, Git, `uv`, and an authenticated Codex CLI with native
plugin support. Run these commands from the downloaded repository in PowerShell;
the default schedule uses the current user's Windows Task Scheduler task.

```powershell
$Repo = (Get-Location).Path
$Data = Join-Path $env:LOCALAPPDATA 'MindIEAgent'
$Bootstrap = Join-Path $Data 'codex-bootstrap\runtime'
$Config = Join-Path $Data 'codex.json'
$UpdaterSettings = Join-Path $Data 'updater.json'
New-Item -ItemType Directory -Force -Path $Data | Out-Null
uv venv --python 3.11 $Bootstrap
uv pip install --python (Join-Path $Bootstrap 'Scripts\python.exe') `
  -r (Join-Path $Repo 'runtime-requirements.txt')
$env:MINDIE_AGENT_CONFIG = $Config
& (Join-Path $Bootstrap 'Scripts\python.exe') `
  (Join-Path $Repo 'plugins\mindie-agent\scripts\setup.py') install `
  --knowledge-python (Join-Path $Bootstrap 'Scripts\python.exe') `
  --config $Config --root $Data
& (Join-Path $Bootstrap 'Scripts\python.exe') `
  (Join-Path $Repo 'plugins\mindie-agent\scripts\auto_update.py') `
  --settings $UpdaterSettings enable --source-root $Repo `
  --root (Join-Path $Data 'updates') --channel main --schedule auto
& (Join-Path $Bootstrap 'Scripts\python.exe') `
  (Join-Path $Data 'updates\launcher.py') $UpdaterSettings status
```

The final command validates and installs the plugin through Codex's native
plugin API before registering the task. To install and validate it without a
periodic task, use `--schedule manual`; this mode persists and reports the
manual check command. Windows CI exercises the pinned runtime and process
contracts. A native Codex install and real Windows host acceptance still need
to be recorded separately.

## Install on WSL/Linux

Use the same runtime prerequisites and shell setup as macOS. On Linux the
default schedule is a per-user systemd timer, enabled only when that user's
systemd manager responds. In WSL, this requires systemd to be enabled and the
user manager to be available. If that scheduler is unavailable, use
`--schedule manual`; it still performs the full runtime probe, native plugin
install and selection readback, then saves a manual scheduling state.

```sh
MINDIE_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/mindie-agent"
MINDIE_BOOTSTRAP="$MINDIE_DATA/codex-bootstrap/runtime"
MINDIE_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/mindie-agent/codex.json"
MINDIE_UPDATER_SETTINGS="${XDG_CONFIG_HOME:-$HOME/.config}/mindie-agent/updater.json"
uv venv --python 3.11 "$MINDIE_BOOTSTRAP"
uv pip install --python "$MINDIE_BOOTSTRAP/bin/python" -r runtime-requirements.txt
MINDIE_AGENT_CONFIG="$MINDIE_CONFIG" \
  "$MINDIE_BOOTSTRAP/bin/python" plugins/mindie-agent/scripts/setup.py install \
  --knowledge-python "$MINDIE_BOOTSTRAP/bin/python" \
  --config "$MINDIE_CONFIG" --root "$MINDIE_DATA"
MINDIE_AGENT_CONFIG="$MINDIE_CONFIG" \
  "$MINDIE_BOOTSTRAP/bin/python" plugins/mindie-agent/scripts/auto_update.py \
  --settings "$MINDIE_UPDATER_SETTINGS" enable --source-root "$PWD" \
  --root "$MINDIE_DATA/updates" --channel main --schedule auto
"$MINDIE_BOOTSTRAP/bin/python" "$MINDIE_DATA/updates/launcher.py" \
  "$MINDIE_UPDATER_SETTINGS" status
```

Change the final option to `--schedule manual` on systems without a responding
per-user systemd manager. Automatic Linux checks run as the same user every
five minutes while that manager is running.

## Everyday use and task boundaries

Use knowledge and remote-dev when they help the current task. Ordinary tasks
need no explicit Skill invocation, activation command, status check or closing
step. The host supplies task identity; public tool arguments carry no identity
or capture token. Queries and block reads verify native identity without
creating a capture binding. Remote-dev remains independent of capture.

The installation's saved contribution choice and approved project scope govern
capture. An eligible Stop event automatically associates its current native
task after checking the named transcript's profile location, session identity,
project directory and creation time. It reads no other session. The contribution
enablement boundary and task creation boundary preserve the first authorized
turn without backfilling older material. Forks exclude inherited parent text.

Existing approved settings persist across tasks and updates. Missing required
configuration and internal failures remain machine diagnostics for the Agent;
they do not initiate a setup conversation. An explicit disable or task revocation
remains authoritative. Repeated events keep the existing binding and do not
replay captured material. Optional configuration and operator commands remain
available when requested. See [capture and operation lifecycle](plugins/mindie-agent/skills/mindie-agent/references/activation-lifecycle.md).

## Optional experience sharing

With contribution off, there is no Stop capture, transcript reading, draft
creation or organizer invocation. Plugin and public knowledge updates continue.

With contribution on, an admitted Stop delivery saves the current task's user
messages, public assistant progress and final answers in their original order.
Users can explicitly [contribute selected historical transcripts](docs/history-import.md).
This is separate from automatic Stop capture; it never scans past sessions
automatically and reuses the saved contribution choice and project scope.

Tools, hidden reasoning, injected instructions and inherited task history are
excluded. A local Gitleaks scanner and privacy rules redact the retained text
before storage or the required summary call. No model writes or rewrites the
body. One task appends to one record within its authorized sharing generation;
the body and read position commit together. The publication path checks scope
and sensitive content before sending a Markdown contribution PR; raw
transcripts are not uploaded. The existing external Grok Bot application owns
repository review and merge. This plugin does not install a Grok CLI reviewer.

The shared core retains small receipts for uncertain writes. An exact confirmed
PR receipt permits send staging to be cleared; a matching published feed is
required before the corresponding local candidate is retired. Continuation uses
confirmed current remote material plus unsent blocks; it cannot restore text
removed by a maintainer. A changed open PR head is an explicit conflict until
its contribution can be reconciled safely. PR construction
and receipt reconciliation use no model. Unknown writes are inspected before
any explicit retry. Optional up/down feedback is never required to finish a task.

The default knowledge source is `mindie-agent/knowledge-vllm-ascend`, branch
`main`. Feed sync remains available with contribution off and retains the last
valid data when an update fails. Retrieval results are references, not authority.

## Local diagnostics and optional fault reporting

Internal failures retain structured incident, stage, code and record references.
A bounded diagnostic projection accompanies the next naturally occurring
capability response for the Agent; delivery is acknowledged only after that
response is written. If no further call occurs, the incident remains pending.
No user status message, diagnostic panel, command or additional model turn is
required. Business results and remote job identity remain intact, including a
separate accounting or cleanup failure after completed work.

Automatic Issue reporting is a separate saved opt-in from community contribution;
installation does not enable uploads. For explicitly requested configuration or
diagnosis, the Agent can use `bridge.py reporting-status`, `reporting-enable` or
`reporting-disable` through the selected installed entrypoint. Enabling reports
returns the selected runtime ensure command; its execution and health readback
belong to that requested operation. Saving the setting alone is not service
readiness. `not_configured` describes upload consent, not an absence of local
logs. The shared choice applies across adapters; disabling uploads preserves
local diagnostics.

The shared reporter uses bounded structured evidence without a model. Business
nonzero exits, permissions, normal network failures and cancellation are not
reported as product defects. Existing updater checks perform offline retention
with reporting off. See the [shared diagnostics contract](https://github.com/mindie-agent/diagnostics).

## Updates and failures

The macOS LaunchAgent, Windows Task Scheduler task, or Linux systemd user timer
checks the adapter's remote `main` and public knowledge feed every five
minutes. On unsupported systems, or when Linux has no responding per-user
systemd manager, explicit `--schedule manual` mode still installs the validated
plugin and records that no scheduler is registered. This work does not open
model tasks or grant contribution authority. Runtime dependencies follow exact reviewed commits in
`runtime-requirements.txt`; code, interpreter, configuration and plugin files
switch as one generation.

Only actual in-flight calls or maintenance postpone switching. Idle task grants
and unresolved receipts do not block an update. Native definitions retain an installation-level launcher path. Selection and
generation lease acquisition share the update lock; an executing process keeps
its selected generation until exit. Remote job state remains available, and
new tool definitions refresh according to the native host lifecycle. Interrupted installation restores the prior configuration
and native package, with readback. Changed Hook dependencies require fresh
native trust. Generation cleanup keeps current and candidate pointers, interrupted
transactions and active process leases. Untracked directories are reported and
retained; elapsed age alone never authorizes deletion.

Business MCP calls and each admitted Stop delivery get one attempt, without an
automatic model retry. The adapter adds no default execution deadline to a
Stop event or capability call. Stop emits the inert protocol response and keeps
internal failures in durable Agent diagnostics; it cannot request another model
turn. Input/output byte limits, concurrency limits, owner cancellation and
process-tree cleanup remain enforced. Explicit remote execution limits follow
the requested operation; long remote work uses owned jobs with status and
cancellation.

Setup installs a checksum-pinned Gitleaks release once. Stop processing performs
no downloads and has no model dependency. A missing or failed scanner leaves
the input unread instead of publishing unredacted content. Public messages are
not shortened to fit a model; the parser advances at complete message boundaries.

Setup and updates install the required LangMem index worker with the explicit
`gpt-5.6-luna` / `low` native policy. It reads complete new blocks plus short
previous navigation and returns block titles/summaries and updated navigation;
it cannot rewrite source bodies. Blocks are at most 16 KiB and each serialized
user prompt is at most 64 KiB. The worker and its identity check have no default
execution deadline; service shutdown or revoked authority cancels their owned
processes. Bounded waits after a terminal result concern cleanup only, and
cleanup failure does not erase the completed result. Native system/context
overhead is included in recorded usage.
Returned output survives local processing failures without another paid call.
Failed or uncertain model calls require explicit retry and retain prior usage.
Missing worker configuration blocks publication and remains a visible failure.
Consumers reuse producer indexes without model calls. Explicit historical import
uses the same incremental pipeline; there is no final full-transcript merge.
See [the transcript contract](docs/public-transcript.md).

The updater records preparation and install outcomes. A known temporary
network failure keeps the installed generation and is retried by the existing
schedule, with backoff. Incompatible content stays quarantined. A certificate
failure is a local trust problem, not bad content, and is retried after
backoff; TLS validation stays enabled. From any
directory, use the stable launcher (set the two variables as in the
installation example in a new shell):

```sh
"$MINDIE_BOOTSTRAP/bin/python" "$MINDIE_DATA/updates/launcher.py" \
  "$HOME/.config/mindie-agent/updater.json" status
"$MINDIE_BOOTSTRAP/bin/python" "$MINDIE_DATA/updates/launcher.py" \
  "$HOME/.config/mindie-agent/updater.json"
```

The second command checks for updates. It takes no operation argument, which
is what the scheduler runs. `status`, `disable`, and `uninstall` are the other
launcher operations; `uninstall` accepts `--purge` only. These dispatch to the
committed generation, not a stale controller copy. Current development tracks
`main`. Release tracking can be selected when a suitable release exists; old
business Skill migration is not part of this update.

Before the repositories collectively meet the product release criteria and
publish normal release versions, destructive state format updates may repeat.
There is no one-time reset allowance. Development package numbers and Git pins
are not a product release declaration. The release commit sets normal
`major.minor.patch` `RELEASE_VERSION` values in the knowledge and receipt layout
modules; they remain `None` during development.
Candidate validation rejects a normal plugin version unless both declarations
match it. The release channel also requires a normal tag and the matching
source version, including cached candidates; a development build cannot enter
that channel with unprotected state.

Knowledge state and remote request receipts have independent format
directories. A development format change selects fresh state while preserving
configuration and the old directories. Released state remains protected even
when a later development build uses it. Compatible updates retain material,
cursors, paid model attempts and consumed remote requests, including uncertain
external writes. An incompatible released format requires an explicit
migration in that candidate; until one exists, the updater rejects it before
retiring the old service, writing an installation transaction or replacing the
native package. It does not clear the state and report a successful upgrade.
This is a release compatibility boundary, not an automatic beta launch or a
claim that future migrations or native release acceptance have been completed.

## Stop or uninstall

Task deactivation revokes that task's automatic capture binding. Sharing-disable stops
contribution for its configured scope; neither removes the plugin nor stops
model-free updates. To stop automatic updates, use the stable launcher:

```sh
"$MINDIE_BOOTSTRAP/bin/python" "$MINDIE_DATA/updates/launcher.py" \
  "$HOME/.config/mindie-agent/updater.json" disable
```

To remove the updater, run the same command with `uninstall` instead of
`disable`. Add `--purge` only to that `uninstall`, and only when no live
generation or recovery journal remains. Then remove MindIE Agent in Codex's
Plugins UI. Updater uninstall does not uninstall the native plugin. A failed
schedule cancellation preserves
its files and reports failure. Close tasks using the plugin before removal;
keep retained runtimes, receipts and caches while old entries may use them.
Do not recursively delete the shared data directory. Removing one adapter does
not revoke or remove the shared reporter; disable reporting separately only
when you want that choice to apply to all adapters.

See [framework stability and verification](docs/framework-stability.md) for the
current behavior, reproducible checks and acceptance boundaries.

## Evidence and follow-up

[Earlier native acceptance](docs/acceptance-lifecycle-2026-09-21.md) records
Luna/max installation, explicit read-only use, configuration independence,
update/job continuity and the remaining contribution/host checks.
[Remote acceptance](docs/remote-general-acceptance-2026-09-20.md) records the
bounded real NPU scope. Earlier reports remain historical evidence for their
recorded revisions. Component checks are diagnostic and do not replace native
or hardware acceptance.

```sh
# Runtime pins are declared once in runtime-requirements.txt. Contract tests
# archive those exact commits and name the missing checkout variable on failure.
export MINDIE_CORE_REPO=/path/to/knowledge-checkout   # contains the declared core commit
export MINDIE_KIMI_REPO=/path/to/kimi-adapter-checkout  # contains 90f73e76c6087ce091570f2d151b709145c913bc
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m unittest tests.test_parallel_codex_contract -v
```

Merging this pre-release adapter permits integrated main-branch and Windows
work to proceed. It is distinct from declaring the entire contribution/Bot
loop or a public release accepted.
