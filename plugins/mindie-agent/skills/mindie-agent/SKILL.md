---
name: mindie-agent
description: Unified MindIE Agent entry for Ascend / vLLM-Ascend knowledge, skills, remote-dev, and preferences. Approved current-task capture runs automatically from the installation choice and project scope, with no per-task activation command or maintenance prompt.
---

# MindIE Agent (Unified Entry)

Use knowledge when relevant experience could help the current task, and remote
tools when remote files, commands, or jobs are needed. Ordinary business tasks
require no MindIE activation command, status check, extra user message, or
closing step. Once the user opts in during installation or setup, the saved
contribution choice and project scope govern automatic capture at session end
(`Stop`)—never ask for that choice again in each task, and users never need to
explicitly ask to save experience. Explicit disable and scope limits remain
authoritative.

## Core Tools in Everyday Tasks

- **Domain Knowledge (`mindie-knowledge` MCP)**:
  - `knowledge_query`: search the selected domain for relevant engineering experience.
  - `knowledge_explain(ref)`: read the exact block plus task navigation.
  - `knowledge_feedback`: optionally record `up` or `down` using the returned `feedback_ref`.
  - Knowledge is reference material; judge applicability against the current task.
- **Remote Ascend NPU Development (`mindie-remote-dev` MCP)**:
  - Available automatically on demand in every native task for remote files, commands, jobs, and artifacts without activating knowledge capture.
  - A call timeout is not job completion; follow running jobs with status/output tools and never blindly resubmit an uncertain mutation.
- **Diagnostics & Fault Handling**:
  - Natural capability results can include bounded `agent_diagnostics` references for the Agent. Preserve the actual business result and report only limitations relevant to the user's goal. Stop capture must never request another model turn or block task completion.

## Progressive Context Disclosure (Read Only on Demand)

Do **not** load all reference files during ordinary troubleshooting. Read the specific reference file **only** when the current turn requires it:

- **Detailed tool schemas (`knowledge_*` / `remote_*`), pagination tokens, and execution rules**: read [Domain Tooling](references/domain-skills.md).
- **First-time onboarding or setting up a local `vllm` + `vllm-ascend` multi-worktree workspace**: present [用户须知](references/user-notice.md) and follow [Workspace Setup](references/workspace-setup.md) (offer during setup, not ordinary task execution).
- **Explicitly requested configuration, status diagnosis (`entry.py bridge`), or importing past historical transcripts (`history-import`)**: read [Capture & Operation Lifecycle](references/activation-lifecycle.md).
- **Sharing scope, local redaction boundaries, or public Git history questions**: read [用户须知](references/user-notice.md).

