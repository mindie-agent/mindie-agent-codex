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

After presenting the user notice during setup, recommend
[mindie-agent/vllm-ascend-workspace](https://github.com/mindie-agent/vllm-ascend-workspace.git)
and ask whether to create a working directory. After creation, ask whether to
initialize its source submodules and local configuration. Creation and
initialization are optional; reuse the user's existing choices. See
[workspace setup](plugins/mindie-agent/skills/mindie-agent/references/workspace-setup.md).

The plugin provides one informational entry Skill, three knowledge tools
(query, block reading and optional feedback), and general remote-dev tools.
It owns Codex identity, transcript parsing, Hook translation and model execution.
The shared knowledge runtime owns retrieval, task admission, contribution
receipts and local cleanup. Claude Code and Kimi have independent adapters.
Old business Skills and profiling remain deferred.

## Install

Requires Git, `uv`, and an authenticated Codex CLI with native plugin support.
On native Windows, Git for Windows also supplies the supported SSH executable.
Use Linux-native Codex, Git and uv inside WSL, with profile, SQLite and runtime
files on the Linux filesystem. Do not substitute Windows executables through
`/mnt/c`.

The first ordinary release is `0.0.1` (`v0.0.1`). It is still being prepared;
these release commands become usable only after the release catalog exists:

```sh
codex plugin marketplace add https://github.com/mindie-agent/mindie-agent-codex.git --ref stable
codex plugin add mindie-agent@mindie-agent --json
```

The `stable` Git catalog must name the exact released plugin commit. Candidate
acceptance instead selects an explicit candidate Git ref; it does not track
unreleased `main`. Native Codex owns plugin registration, refresh and removal.
There is no MindIE code-update daemon or separate release resolver.

SessionStart and MCP initialization prepare the plugin's own
[pyproject.toml](plugins/mindie-agent/pyproject.toml) and
[uv.lock](plugins/mindie-agent/uv.lock) with `uv sync --locked --no-editable`.
uv manages Python 3.13, dependency resolution, installation locking and its
artifact cache. Preparation is not tool readiness: an interrupted or failed
initialization remains unavailable until a later successful initialization.
Stop never downloads Python or installs a runtime.
The declared native MCP startup window is ten minutes, including first-use
downloads/builds and waiting for concurrent preparation. This does not change
tool-call deadlines. A failed preparation retains its safe stage and a private
`prepare-*.log` beside the release entry; no tools are advertised as ready.

[The product declaration](plugins/mindie-agent/product-contract.json) binds the
public content contract. Preparation verifies exact installed Git revisions,
runtime APIs, the publication declaration, and installed adapter bytes. The
complete runtime lives outside the disposable native plugin cache. Its key
includes plugin/lock bytes and Python ABI/platform; the release entry is fixed
under `PLUGIN_DATA/releases/0.0.1/entry.py`. Preparation cannot overwrite an
existing release entry/runtime with different bytes. An illegal same-version
republish does not activate its new bytes: warm entries continue their verified
binding, while explicit preparation detects the conflict. A corrupt completed
runtime is not silently repaired in place. Warm entries do not hash the
disposable native cache. Existing sessions retain their fixed release runtime.

Default adapter configuration is `~/.config/mindie-agent/codex.json`; persistent
knowledge data is `~/.local/share/mindie-agent`. `MINDIE_AGENT_CONFIG` selects an
explicit profile. Sharing starts off/unconfigured. Existing consent and project
scope are reused; installation never grants native Hook trust or permissions.
Use the host's trust controls and sign-in. A selected provider's secrets are
never copied into a runtime receipt or configuration-selection pointer.

Codex 0.153.4's legacy MCP schema supports declared environment names but does
not provide `PLUGIN_DATA`. One launcher contains the tested fixed-layout
fallback under `CODEX_HOME/plugins/{data,cache}`; it never scans versions or
selects a newest directory. The portable MCP schema cannot preserve arbitrary
provider environment names on this host. SessionStart can supply that context;
an authenticated live exact service can be reused. If it dies and the MCP
process lacks a required custom provider key, the cold call fails explicitly and
requests a new native session. It never silently selects another provider.

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
reported as product defects. SessionStart performs offline retention
with reporting off; the diagnostics component owns reporter scheduling. See the [shared diagnostics contract](https://github.com/mindie-agent/diagnostics).

## Updates and failures

Codex refreshes configured Git marketplaces according to its native lifecycle.
An explicit refresh uses:

```sh
codex plugin marketplace upgrade mindie-agent --json
```

MindIE does not wrap this command in another native mutation transaction.
Every published version has immutable bytes. SessionStart and explicit `sync`
refresh public knowledge through the shared Feed's interval/backoff rules;
there is no promise of a separate five-minute refresh during a long session.
Local content remains reference material, and retained stale content is labeled.

The installed adapter configuration names one exact runtime and engine. Ordinary
SessionStart, MCP and Stop may prepare a genuinely empty profile, but never
replace a different existing installation. A loaded MCP process verifies its
installation identity before each tool call; changed bindings produce
`reload_required` before any tool side effect. Reloading code is not a request
to authorize capture again: saved contribution scope, disabled choice and
reporting choice remain independent user data.

This first, unreleased 0.0.1 does not migrate development updater generations,
scheduled tasks, transaction journals or inferred consent. Unsupported existing
layouts fail visibly without changing their files. Their data, paid-attempt and
unknown-operation receipts remain evidence. Future released data formats and
interfaces require compatibility decisions based on their actual contracts;
product version ordering is not a service-routing protocol.

Transcript bodies remain complete and mechanically redacted. The summary worker
has no default total execution deadline; cancellation and ownership loss remain
visible. A completed model result and its usage survive cleanup/accounting
failure. Failed or uncertain paid work is never automatically replayed. Native
and background acceptance is separate from unit tests or runtime preparation.

## Stop or uninstall

Use the native plugin controls to disable or remove the plugin. On the verified
Codex versions the removal command is:

```sh
codex plugin remove mindie-agent@mindie-agent --json
```

Close tasks using the plugin and retire its exact knowledge service before
removal. Native removal clears plugin installation/cache, not the shared
knowledge data, consent, paid-attempt receipts or fixed runtimes. Retain those
while a loaded entry or pending operation may use them; no automatic purge or
new runtime garbage collector is introduced. Disable shared fault reporting
separately only when that is the intended choice for all adapters.

For optional operator commands, use the fixed release `entry.py` with `bridge`
and the existing command, for example `bridge sharing-disable`, `bridge status`
or `bridge history-import --source /absolute/transcript.jsonl`. These are not
prerequisites for ordinary tasks. `sync` performs an explicit model-free feed
refresh; `configure` preserves the existing profile's sharing semantics.

## Evidence and follow-up

[Earlier native acceptance](docs/acceptance-lifecycle-2026-09-21.md) records
Luna/max installation, explicit read-only use, configuration independence,
update/job continuity and the remaining contribution/host checks.
[Remote acceptance](docs/remote-general-acceptance-2026-09-20.md) records the
bounded real NPU scope. Earlier reports remain historical evidence for their
recorded revisions. Component checks are diagnostic and do not replace native
or hardware acceptance.

```sh
# Runtime pins are declared by the plugin pyproject.toml and uv.lock. Contract tests
# archive those exact commits and name the missing checkout variable on failure.
export MINDIE_CORE_REPO=/path/to/knowledge-checkout   # contains the declared core commit
export MINDIE_KIMI_REPO=/path/to/kimi-adapter-checkout  # contains 90f73e76c6087ce091570f2d151b709145c913bc
uv run --project plugins/mindie-agent --no-sync python -m unittest discover -s tests -p 'test_*.py'
uv run --project plugins/mindie-agent --no-sync python -m unittest tests.test_parallel_codex_contract -v
```

Merging this pre-release adapter permits integrated main-branch and Windows
work to proceed. It is distinct from declaring the entire contribution/Bot
loop or a public release accepted.
