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
plugin support. From this repository:

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r runtime-requirements.txt
python3 plugins/mindie-agent/scripts/setup.py install --knowledge-python "$PWD/.venv/bin/python"
python3 plugins/mindie-agent/scripts/auto_update.py enable --source-root "$PWD" --channel main
python3 plugins/mindie-agent/scripts/auto_update.py status
```

Setup writes MindIE-owned configuration and defaults community contribution to
off. The updater packages the source, installs through the native Codex plugin
API, verifies the selected version and registers the macOS update schedule.
Retain the configured initial interpreter until a managed runtime replaces it.
The updater never resets or stashes a developer checkout.

Review and trust a changed Stop Hook in the native Codex UI when you want it to
run, then start a new task. Hook trust belongs to the user; installation and
updates do not grant it. Use Codex's native permission controls for tools.

Default configuration is `~/.config/mindie-agent/codex.json`; data is under
`~/.local/share/mindie-agent`. For a separate installation, supply `--config`
to setup and `MINDIE_AGENT_CONFIG` to the one-time updater enable command.
The generated Skill, MCP and Hook entries retain that configuration binding;
ordinary later tasks do not need to export it.

Windows installation, process handling and Task Scheduler paths are implemented.
The user will validate and advance them on a dedicated Windows machine **after
this change is merged**. Windows hardware acceptance is not this merge's gate.

## First use and task boundaries

Explicitly invoke `$mindie-agent`. Its offline status offers three choices:
recommended public contribution for a named project/repository/account,
read-only knowledge, or configuration later. There is no default consent.
The choice persists, and the Skill activates only this native task.

Knowledge tools require that explicit activation. The host supplies task
identity; public knowledge calls have no identity or capability argument.
Mentioning MindIE, opening a repository, MCP discovery or a query does not
activate it. Remote-dev works in ordinary native tasks independently.

Authorization persists across idle time and runtime updates. There is no
24-hour renewal requirement and no configuration fingerprint expiry.
Revocation, a paused task or an actual project-scope change still matters.
Use the entry Skill to deactivate or explicitly recover a paused task.
See [activation details](plugins/mindie-agent/skills/mindie-agent/references/activation-lifecycle.md).

## Optional experience sharing

With contribution off, there is no Stop capture, transcript reading, draft
creation or organizer invocation. Plugin and public knowledge updates continue.

With contribution on, an admitted Stop delivery records the current task's
eligible public material for a separate, bounded local organizer. Hidden
reasoning and inherited task history are excluded. The publication path checks
scope and sensitive content before sending a Markdown contribution PR; raw
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

The shared reporter uses bounded structured evidence without a model. Business
nonzero exits, permissions, normal network failures and cancellation are not
reported as product defects. Existing updater checks perform offline retention
with reporting off. See the [shared diagnostics contract](https://github.com/mindie-agent/diagnostics).

## Updates and failures

The macOS LaunchAgent checks the adapter's remote `main` and public knowledge
feed every five minutes. This work does not open model tasks or activate
knowledge. Runtime dependencies follow exact reviewed commits in
`runtime-requirements.txt`; code, interpreter, configuration and plugin files
switch as one generation.

Only actual in-flight calls or maintenance postpone switching. Idle task grants
and unresolved receipts do not block an update. Old loaded entrypoints and
remote job state remain available; new definitions refresh according to the
native host lifecycle. Interrupted installation restores the prior configuration
and native package, with readback. Changed Hook dependencies require fresh
native trust. No live entrypoint is automatically deleted.

Business MCP calls and each admitted Stop delivery get one attempt, without an
automatic model retry. The Stop Hook has a 1.5-second inner budget and a 2-second
native timeout, always returns normal completion and cannot request another
model turn. Missing/offline capture does not create an automatic repair task.
MCP calls have bounded input, output and process deadlines; long remote work
uses owned jobs with explicit status and cancellation.

Optional organizer work is isolated from tools, plugins and other agents and
has a 120-second model-process deadline. The shared runtime limits background
admission per rolling hour and pauses consecutive failures. These limits bound
optional background cost; they neither expire ordinary task authorization nor
require a user-facing completion checklist. Exact status and recovery guidance
are available through the entry Skill.

The updater records bounded preparation/install attempts and concrete failure
reasons. Inspect it with `auto_update.py status`; disable its scheduling with
`auto_update.py disable`. Current development tracks `main`. Release tracking
can be selected when a suitable release exists; old business Skill migration
is not part of this update.

## Evidence and follow-up

[Current native acceptance](docs/acceptance-lifecycle-2026-09-21.md) records
Luna/max installation, explicit read-only use, configuration independence,
update/job continuity and the remaining contribution/host checks.
[Remote acceptance](docs/remote-general-acceptance-2026-09-20.md) records the
bounded real NPU scope. Earlier reports remain historical evidence for their
recorded revisions. Component checks are diagnostic and do not replace native
or hardware acceptance.

```sh
.venv/bin/python -m unittest discover -s tests
```

Merging this pre-release adapter permits integrated main-branch and Windows
work to proceed. It is distinct from declaring the entire contribution/Bot
loop or a public release accepted.
