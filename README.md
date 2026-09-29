<p align="center">
  <img src="https://raw.githubusercontent.com/mindie-agent/mindie-agent/main/assets/brand/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

# MindIE Agent · Codex

A native Codex plugin for NPU infrastructure work, initially the `vllm-ascend`
domain. It inherits the [nine VAWS / MindIE principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md).
The native task remains in charge; knowledge is optional reference material.

The plugin provides one explicit entry Skill, three knowledge tools
(query, full-body read and optional feedback), and general remote-dev tools.
It owns Codex identity, transcript parsing, Hook translation and model execution.
The shared knowledge runtime owns retrieval, task admission, contribution
receipts and local cleanup. Claude Code and Kimi have independent adapters.
Old business Skills and profiling remain deferred.

## Install on macOS

Requires Python 3.11+, Git, `uv`, and an authenticated Codex CLI with native
plugin support.

The knowledge interpreter also needs SQLite 3.43.0 or newer with FTS5 and
`contentless_delete` support. Installation checks the actual SQLite library;
the Python version alone does not establish this capability.

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

## First use and task boundaries

Explicitly invoke `$mindie-agent:mindie-agent` (the installed plugin's qualified
Skill name). Its status distinguishes task binding from
experience capture. Missing public destination and project scope are incomplete
configuration; supply only those missing values through `bridge.py config`.
Existing approved settings persist across tasks and updates. Configuration in
an already-bound task prepares capture without a second activation.
An explicit disable and legacy declined settings remain disabled until changed.
Read-only/later product modes are removed; they are retained only as migration
data. Inspect actual capture, organization and contribution receipts before
claiming full-loop success.

Knowledge tools require that entry binding. The host supplies task
identity; public knowledge calls have no identity or capability argument.
Mentioning MindIE, opening a repository, MCP discovery or a query does not
bind it. Remote-dev works in ordinary native tasks independently.

The install-level choice and the task binding persist across idle time,
restarts, runtime updates and failures. There is no renewal requirement, no
configuration fingerprint expiry and no failure-count pause — ordinary
failures never require deactivate/reactivate. Only explicit unbinding or an
actual project-scope change ends a binding.
See [activation details](plugins/mindie-agent/skills/mindie-agent/references/activation-lifecycle.md).

## Optional experience sharing

With contribution off, there is no Stop capture, transcript reading, draft
creation or organizer invocation. Plugin and public knowledge updates continue.

With contribution on, an admitted Stop delivery saves the current task's user
messages, public assistant progress and final answers in their original order.
Tools, hidden reasoning, injected instructions and inherited task history are
excluded. A local Gitleaks scanner and privacy rules redact the retained text
before storage or any optional summary call. No model writes or rewrites the
body. One task appends to one record within its authorized sharing generation;
the body and read position commit together. The publication path checks scope
and sensitive content before sending a Markdown contribution PR; raw
transcripts are not uploaded. The existing external Grok Bot application owns
repository review and merge. This plugin does not install a Grok CLI reviewer.

The shared core retains small receipts for uncertain writes. Once the exact PR
receipt is confirmed, submitted local bodies can be removed; a later task
continuation retrieves the exact earlier revision when needed. PR construction
and receipt reconciliation use no model. Unknown writes are inspected before
any explicit retry. Optional up/down feedback is never required to finish a task.

The default knowledge source is `mindie-agent/knowledge-vllm-ascend`, branch
`main`. Feed sync remains available with contribution off and retains the last
valid data when an update fails. Retrieval results are references, not authority.

## Local diagnostics and optional fault reporting

Tool failures include an incident reference and a concrete local status command.
The original result, remote job identity and cancellation behavior remain intact.
`bridge.py reporting-status` reads local faults, authorization and worker health;
it does not activate knowledge, install a service or retry work.

Automatic Issue reporting is a separate opt-in from community contribution.
`bridge.py reporting-enable` saves the choice and returns the exact selected
runtime command to ensure the shared reporter outside the Hook deadline.
`bridge.py reporting-disable` revokes pending publication. First-use status
explains this independent choice; no upload is enabled by installation.
Ask the entry Skill to run these commands through the selected installed
entrypoint. Execute the returned ensure command once outside the Hook, then
check that the runtime is ready and the worker is healthy. Saving the setting
alone is not service readiness. `not_configured` describes upload consent;
it does not mean there are no local logs. The shared setting applies across
MindIE adapters; disabling it preserves local diagnostics.

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
model tasks or activate knowledge. Runtime dependencies follow exact reviewed commits in
`runtime-requirements.txt`; code, interpreter, configuration and plugin files
switch as one generation.

Only actual in-flight calls or maintenance postpone switching. Idle task grants
and unresolved receipts do not block an update. Old loaded entrypoints and
remote job state remain available; new definitions refresh according to the
native host lifecycle. Interrupted installation restores the prior configuration
and native package, with readback. Changed Hook dependencies require fresh
native trust. No live entrypoint is automatically deleted.

Business MCP calls and each admitted Stop delivery get one attempt, without an
automatic model retry. The Stop Hook has a 1.5-second inner budget on POSIX and
1.3 seconds on Windows, within the 5-second native timeout. It always returns
normal completion and cannot request another model turn. Missing/offline
capture does not create an automatic repair task.
MCP calls have bounded input, output and process deadlines; long remote work
uses owned jobs with explicit status and cancellation.

Setup installs a checksum-pinned Gitleaks release once. Stop processing performs
no downloads and has no model dependency. A missing or failed scanner leaves
the input unread instead of publishing unredacted content. Public messages are
not shortened to fit a model; the parser advances at complete message boundaries.

Setup and updates automatically install the optional title/summary worker.
The adapter owns its fixed GPT-6-Luna / low policy; there is no user-facing
model or effort setting and no inheritance from the business task. A separate
worker reads the complete redacted body. It can only update title and summary;
it cannot rewrite the body or hold up later captures. The native invocation
deadline is 35 seconds, with a 45-second outer cancellation deadline. A failed
or superseded attempt leaves the source excerpt and full body available.
Updates replace obsolete model arguments with the installed adapter policy.
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

## Stop or uninstall

Task deactivation ends that task's knowledge access. Sharing-disable stops
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
# Pins match .github/workflows/tests.yml. Do not omit these: the contract
# tests archive the commits below and fail, naming the variable, if unset.
export MINDIE_CORE_REPO=/path/to/knowledge-checkout   # contains 68ed86579bcbf88ac8ed2817a2ca81756c351485
export MINDIE_KIMI_REPO=/path/to/kimi-adapter-checkout  # contains 90f73e76c6087ce091570f2d151b709145c913bc
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m unittest tests.test_parallel_codex_contract -v
```

Merging this pre-release adapter permits integrated main-branch and Windows
work to proceed. It is distinct from declaring the entire contribution/Bot
loop or a public release accepted.
