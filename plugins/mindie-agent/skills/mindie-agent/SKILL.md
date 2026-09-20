---
name: mindie-agent
description: Manually activate MindIE Agent knowledge and optional community sharing for this Codex session when the user explicitly invokes this skill for a vLLM-Ascend task.
---

# MindIE Agent

Use this entry when the user explicitly invokes MindIE Agent for the current
native Codex task. Discussing the plugin or working in a relevant repository
does not activate it. The configured domain is initially `vllm-ascend`.

Run `python3 ../../scripts/bridge.py activate`, resolving the script to an
absolute path relative to this SKILL.md directory (`python` on Windows).
The native shell supplies `CODEX_THREAD_ID`; do not set or override it.
Activation binds this task to the plugin. MCP calls need no identity or
activation arguments: the host supplies task identity, and the adapter checks
it. Internal activation values are not part of the user-facing response.
On explicit disable, run the same script with `deactivate`.

Activation and community sharing are separate. Sharing is OFF unless the user
has configured it. While off, there is no Stop capture, transcript reading,
draft creation or background model work. Manage it with `sharing-status`,
`sharing-enable` and `sharing-disable`; disabling cancels pending work and
re-enabling only processes newly authorized material. A capture startup failure
does not stop the user's task. Operational details, including expired or paused
activation: [activation lifecycle](references/activation-lifecycle.md).

Use knowledge when prior experience could help the task:

- `knowledge_query` searches the selected domain. It is optional and never a
  prerequisite for capture or ordinary work.
- `knowledge_explain` reads a useful result. Copy its returned `ref` exactly;
  omit the limit for the full body, or use character offsets for pagination.
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
circuit. Preserve unrelated tasks and remote workloads.

The old domain Skill catalogue is retired; profiling analysis remains deferred.
