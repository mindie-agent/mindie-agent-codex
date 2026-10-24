"""[Role] Bind an explicit native UserPromptSubmit gesture to its session.

[Invariants]
- Ordinary prompts return no knowledge context and never read configuration.
- Only the native hook's session/cwd can establish the manual entry grant.
- A later turn keeps the grant; new sessions and subagents never inherit it.

[Timeouts & Locks]
- Input is bounded at 32 MiB; writes use Admission's WAL/5-second lock budget.

[Side-Effect Policy]
- Explicit entry mutates only the existing Admission lease; no knowledge
  service, corpus, model, transcript or contribution worker is started here.
"""
import json
import os
from pathlib import Path
import re

from session_gate import IDENTITY

GESTURE = re.compile(r"^\s*(?:/mindie-agent|\$mindie-agent(?::mindie-agent)?)(?=\s|$)")


def requested(event):
    return (isinstance(event, dict)
            and event.get("hook_event_name") == "UserPromptSubmit"
            and not event.get("agent_id")
            and isinstance(event.get("prompt"), str)
            and GESTURE.match(event["prompt"]) is not None)


def read_event(stream):
    raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("native prompt hook exceeds its byte budget")
    return json.loads(raw)


def action(event):
    return event["prompt"][GESTURE.match(event["prompt"]).end():].strip()


def authorize(adapter, event):
    if not requested(event):
        return {}
    session, scope = event.get("session_id"), event.get("cwd")
    if not isinstance(session, str) or not IDENTITY.fullmatch(session):
        raise ValueError("native prompt hook has no valid session identity")
    if not isinstance(scope, str) or not Path(scope).is_absolute():
        raise ValueError("native prompt hook has no absolute project scope")
    from mindie_knowledge.loop.activation import Admission

    if action(event) == "deactivate":
        from mindie_knowledge.loop.domain_routes import deactivate_registered_domains
        result = deactivate_registered_domains(
            adapter["engine_config"], domain_configs=adapter.get("domain_configs"),
            session=session,
        )
        if result["status"] != "inactive":
            return dict(
                systemMessage="MindIE session deactivation is incomplete; inspect the per-domain result.",
                hookSpecificOutput=dict(hookEventName="UserPromptSubmit", additionalContext=
                    "The user requested session deactivation. Preserve completed revocations and inspect "
                    "the remaining domains; do not claim global completion: " + json.dumps(result)),
            )
        context = "MindIE knowledge is deactivated for this session. Remote-dev remains available."
    else:
        Admission(adapter["admission_path"]).activate(
            session, project_root=str(Path(scope).resolve()), root_session=session,
        )
        context = ("The user explicitly enabled MindIE knowledge for this session. "
                   "Follow the installed mindie-agent skill for this task. Later turns keep this grant; "
                   "background capture additionally requires saved contribution consent and project scope.")
        plugin_root = os.environ.get("PLUGIN_ROOT")
        if plugin_root:
            context += " Read the entry instructions at " + str(
                Path(plugin_root) / "skills/mindie-agent/SKILL.md") + "."
    return dict(hookSpecificOutput=dict(hookEventName="UserPromptSubmit", additionalContext=context))
