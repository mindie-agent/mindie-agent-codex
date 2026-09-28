---
name: mindie-agent
description: The single MindIE Agent entry. Invoke this skill once in a vLLM-Ascend task to bind it internally and use knowledge, optional community sharing, and remote-dev. One-time setup persists for the installation and is never re-asked.
---

# MindIE Agent

This skill is the only entry a user needs (`$mindie-agent`). Invoking it in
the current native Codex task binds the task internally and automatically —
binding reuses the saved install-level choice and is not a consent step.
Discussing the plugin or working in a relevant repository does not bind
anything. The configured domain is initially `vllm-ascend`.

Resolve `../../scripts/bridge.py` to an absolute path relative to this SKILL.md
directory (`python` on Windows). The native shell supplies `CODEX_THREAD_ID`;
do not set or override it. MCP calls need no identity or binding arguments.

Only ever run the copy of this skill and its scripts that lives under the
ACTIVE profile's plugin cache: `$CODEX_HOME/plugins/cache/mindie-agent/...`
when `CODEX_HOME` is set, otherwise `~/.codex/plugins/cache/mindie-agent/...`.
Stale caches from other profiles or older installs can remain on disk and may
surface in searches; never run their scripts, because each installed copy binds
the profile it was installed for. When several copies exist inside the active
cache, use the one with the greatest version. After `activate`, the result's
`scripts` (and `build` when present) identify the generation that actually
ran — check they belong to that same active-cache install before relying on
the binding.

## First explicit invocation

1. Run `python3 <bridge.py> activate`. This one call performs the internal
   binding for the current task and returns status. It does not start a
   model or collect anything by itself.
2. If the result indicates first use (no choice was ever saved), present the
   three choices to the user and wait for their answer. There is no default yes.
   - Recommended: public community contribution for the current named
     project/repository/account. Then run `python3 <bridge.py> config
     --community-repository OWNER/REPO --community-project-root PATH
     --community-visibility public` (optional `--community-account NAME`).
     Do not reinstall and do not hand-edit JSON.
   - Read-only knowledge, no contribution: `python3 <bridge.py> sharing-choice read-only`
   - Configure later: `python3 <bridge.py> sharing-choice later`
3. After a choice is recorded it persists for the installation: new tasks,
   forks, restarts, upgrades and failure counts never re-ask it and never
   revoke it; an explicitly disabled value stays disabled.
4. On explicit disable of contribution, run `python3 <bridge.py> sharing-disable`.
   `python3 <bridge.py> deactivate` only unbinds this task; invoking the
   entry again rebinds it without any setup.

Headless install leaves sharing unconfigured/off. Knowledge retrieval, plugin
updates and remote tools work with sharing off. Sharing off means no Stop
capture, transcript reading, draft creation or background model work.

## Local diagnostics

`python3 <bridge.py> reporting-status` reads local faults and reporter state;
it does not upload or retry anything. Use a returned incident ID to locate the
original failure and its `record_ref`, then continue the user's task as appropriate.

On first configuration, `diagnostics.choice` describes optional automatic tool
fault reporting. Offer it separately from community contribution; leaving it
off does not block the task. Respect an existing choice. If the user enables it,
run `python3 <bridge.py> reporting-enable`, then the returned `command_line`
once outside a Hook to prepare the shared reporter. On disable, run
`python3 <bridge.py> reporting-disable`. Failure to prepare reporting is local
status, not a reason to replay the user's failed operation.

## Sharing and recovery

- Status: `python3 <bridge.py> status` or `init` (offline). This is the normal way to see a sharing problem.
- Toggle recorded sharing: `sharing-status`, `sharing-enable`, `sharing-disable`.
- A transient local or network failure is recovered by the existing worker — a
  deadline-interrupted region gets one bounded background recovery. Do not
  intervene, re-run the model, or run a contribution command for it.
- Authentication, trust, rejected content, or invalid configuration can need an explicit user or operator action.
- Optional troubleshooting of one existing batch, not a recovery step: `contribution-inspect`, `contribution-reconcile`, `contribution-retry`, or `contribution-compact`. Uncertain writes are inspected or reconciled, never blindly retried.

A capture startup failure does not stop the user's task. Operational details:
[activation lifecycle](references/activation-lifecycle.md).

## Knowledge and remote tools

Use knowledge when prior experience could help the task:

- `knowledge_query` searches the selected domain. It is optional and never a
  prerequisite for capture or ordinary work.
- `knowledge_explain` reads one page of a useful result. Copy its returned `ref` exactly. The page is a slice, not the full case. If `next_offset` is an integer and more of that case is relevant, call again with that offset; do not count characters or keep paging once the page is enough.
- `knowledge_feedback` optionally records `up` or `down` for a consulted entry;
  the reason is optional. Silence is not a vote.

Knowledge is reference material. Judge applicability against the current task;
withdrawn references are labeled historical context. No extra report, mandatory
vote, validation form or model judge is required.

If an operation rejects its arguments or a reference before execution, use the
reported reason and declared schema to correct it. Do not repeat unchanged
invalid requests. Service or background-maintenance failure should not create
extra user turns or an automatic repair loop; continue with native capabilities
unless fixing the component is part of the requested work.

`mindie-remote-dev` is available on demand in every native task, including tasks
that have not invoked this Skill. Remote calls do not activate knowledge capture.
Use it for remote files, commands, jobs and artifacts. A call deadline is not a
job-completion result: follow an existing job with the provided status/output
operations as needed for the user's task. If a mutation's result is uncertain,
check its existing receipt or job before deciding what to do; never resubmit it
blindly. Keep polling proportionate to the job and respect cancellation.
Details: [domain tooling](references/domain-skills.md).

Hook and MCP deadlines, duplicate-request checks and background model budgets
are enforced by the runtime. Stop capture must never request another model turn
or block task completion. Do not reactivate a paused task to bypass a failure
circuit; recover with deactivate then activate. Preserve unrelated tasks and
remote workloads.

The old domain Skill catalogue is retired; profiling analysis remains deferred.
