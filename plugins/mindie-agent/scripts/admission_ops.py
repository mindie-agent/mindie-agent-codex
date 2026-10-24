"""Admission operations inside the fixed installed runtime.

[Role]
Executes core `Admission` lease operations (`activate`, `deactivate`, `check`, `active_lease`, `resolve`, `claim`, `finish`, `stop_capture`) inside the configured release interpreter.

[Invariants]
- Native task identity comes strictly from `CODEX_THREAD_ID` in the environment (`native_session()`), never from model arguments.
- Always exits 0 and emits `{"ok": true, "result": ...}` or `{"ok": false, "error": ...}` so the caller (`session_gate.py`) can distinguish a clean domain refusal from a crashed helper.

[Timeouts & Locks]
- Invoked under the caller's `update_lock` (`session_gate.py`) with a 5.0s subprocess timeout (`None` for `stop_capture` handoff).

[Side-Effect Policy]
- Mutates the SQLite admission store (`admission_path`) and, on `stop_capture`, forwards validated transcript references to `mindie_knowledge.loop.cli.capture_hook`.
"""

import json
from collections.abc import Mapping
import os
from pathlib import Path
import re
import sys
from capture_errors import TranscriptSourceError

IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def config_path():
    return (
        Path(
            os.environ.get(
                "MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json"
            )
        )
        .expanduser()
        .absolute()
    )


def read_config():
    """One read of the adapter configuration for this helper invocation."""
    try:
        config = json.loads(config_path().read_text(encoding='utf-8'))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        raise ValueError("MindIE adapter configuration is unreadable")
    if not isinstance(config, dict):
        raise ValueError("MindIE adapter configuration is unreadable")
    return config


def admission(config):
    from mindie_knowledge.loop.activation import Admission

    path = config.get("admission_path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError(
            "adapter configuration lacks an absolute admission_path; "
            "run scripts/setup.py to configure this installation"
        )
    return Admission(path)


def native_session():
    # The native shell supplies this value. Never accept a session ID from the
    # payload or guess one from history; a Fork/subagent has its own identity.
    session = os.environ.get("CODEX_THREAD_ID", "")
    if not IDENTITY.fullmatch(session):
        raise ValueError("Native CODEX_THREAD_ID required for this operation")
    return session


def checked_session(payload):
    session = payload.get("session")
    if not isinstance(session, str) or not IDENTITY.fullmatch(session):
        raise ValueError("Valid MindIE session identity required")
    return session


def checked_token(payload):
    token = payload.get("token")
    if token is not None and (not isinstance(token, str) or not token):
        raise ValueError("MindIE activation token must be nonempty text")
    return token


def operation(name, payload):
    config = payload.get("_adapter") if name == "stop_capture" else None
    config = read_config() if config is None else config
    if not isinstance(config, dict):
        raise ValueError("invalid internal Stop binding")
    if name == "activate":
        root = payload.get("project_root")
        if not isinstance(root, str) or not os.path.isabs(root):
            raise ValueError("project_root must be an absolute path")
        project_root = str(Path(root).resolve())
        session = native_session()
        domain = payload.get("domain")
        if domain is not None:
            # Selection, target consent/scope validation and target Admission
            # activation stay in the shared core helper. The default engine's
            # lease store and token are never touched on this path.
            from mindie_knowledge.loop.domain_routes import activate_selected_domain

            selected = activate_selected_domain(
                config["engine_config"],
                domain=domain,
                domain_configs=config.get("domain_configs"),
                session=session,
                project_root=project_root,
                root_session=None,
            )
            lease = selected["lease"]
            return dict(
                status="active",
                domain=selected["domain"],
                engine_config=selected["engine_config"],
                generation=selected["generation"],
                mindie_session_id=lease["session"],
                mindie_activation=lease["token"],
                activated_at=lease["activated_at"],
                project_root=lease["project_root"],
            )
        store = admission(config)
        # Lineage is unknown at binding: the native task itself is the root.
        # Known inherited Fork/subagent histories are not new scopes. This is
        # the internal identity binding for the entry — never a consent step,
        # and never failure-paused.
        lease = store.activate(
            session,
            project_root=project_root,
            root_session=None,
        )
        return dict(
            status="active",
            mindie_session_id=lease["session"],
            mindie_activation=lease["token"],
            activated_at=lease["activated_at"],
            project_root=lease["project_root"],
        )
    if name == "deactivate":
        session = native_session()
        domain_configs = config.get("domain_configs")
        if domain_configs is not None and not isinstance(domain_configs, Mapping):
            raise ValueError("domain_configs must be a mapping")
        if domain_configs:
            try:
                from mindie_knowledge.loop.domain_routes import deactivate_registered_domains
            except ModuleNotFoundError as exc:
                if exc.name != "mindie_knowledge.loop.domain_routes":
                    raise
                raise ValueError(
                    "multi-domain deactivation is unavailable in the installed knowledge runtime"
                ) from exc
            result = deactivate_registered_domains(
                config["engine_config"],
                domain_configs=domain_configs,
                session=session,
            )
            return dict(result, session_id=session)
        # No configured domains: preserve the installed default-only path and
        # do not import the newer helper.
        store = admission(config)
        store.deactivate(session)
        return dict(status="inactive", session_id=session)
    store = admission(config)
    if name == "check":
        lease = store.check(checked_session(payload), checked_token(payload))
        return dict(lease)
    if name == "active_lease":
        # None is normal absence or explicit revocation. A damaged required
        # authority raises instead; do not infer this distinction from text.
        lease = store.active_lease(checked_session(payload))
        return dict(lease) if lease is not None else None
    if name == "resolve":
        token = checked_token(payload)
        if token is None:
            raise ValueError("MindIE operation requires its internal task-binding token")
        lease = store.resolve(token)
        if lease is None:
            raise ValueError(
                "MindIE task-binding token does not match an active lease"
            )
        return dict(lease)
    if name == "claim":
        kind, identity = payload.get("kind"), payload.get("identity")
        for value, field in ((kind, "kind"), (identity, "identity")):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError(f"invalid claim {field}")
        return dict(
            claimed=bool(
                store.claim(
                    checked_session(payload), kind, identity, checked_token(payload)
                )
            )
        )
    if name == "finish":
        token = checked_token(payload)
        if token is None:
            raise ValueError("finish requires the session activation token")
        store.finish(
            checked_session(payload), token, payload.get("succeeded") is True
        )
        return dict(status="recorded")
    if name == "stop_capture":
        event = payload.get("event")
        if not isinstance(event, dict):
            raise ValueError("stop_capture requires the forwarded Stop event")
        session = event.get("session_id")
        if not isinstance(session, str) or not IDENTITY.fullmatch(session):
            raise ValueError("Valid MindIE session identity required")
        import sharing
        lease = store.knowledge_lease(session)
        if lease is None:
            return dict(stage="inert", reason="manual-entry-required")
        settings = sharing.read(adapter=config)
        if settings is None:
            if sharing.status(adapter=config).get("state") == "disabled":
                return dict(stage="inert", reason="sharing-disabled")
            return dict(stage="unavailable", reason="missing-configuration")
        if not settings['enabled'] or sharing.consent_allows(settings) is False:
            return dict(stage="inert", reason="sharing-disabled")
        import codex_transcript
        source = codex_transcript.capture_source(event.get('transcript_path'), session)
        candidate = dict(project_root=source['project_root'], root_session=session,
                         activated_at=max(settings['enabled_at'], source['created_at']))
        if not sharing.capture_allowed(candidate, None, adapter=config):
            return dict(stage="inert", reason="out-of-scope")
        inspected = store.inspect(session)
        if inspected.get("status") == "unavailable":
            return dict(
                stage="unavailable", reason="admission-unreadable",
                cause="admission-unreadable",
            )
        if inspected.get('status') == 'inactive':
            return dict(stage='inert', reason='task-revoked')
        if lease["project_root"] != source["project_root"]:
            raise ValueError("Stop transcript differs from the manually activated project scope")
        turn = event.get("turn_id")
        if not isinstance(turn, str) or not IDENTITY.fullmatch(turn):
            raise ValueError("invalid hook identity")
        forwarded = dict(event, transcript_path=source['transcript_path'],
                         mindie_activation=lease["token"], harness="codex")
        from mindie_knowledge.loop.cli import capture_hook

        result = capture_hook(config["engine_config"], forwarded)
        return result if isinstance(result, dict) else dict(stage="unavailable", reason="internal")
    raise ValueError("unsupported admission operation")


def main():
    try:
        if len(sys.argv) not in (2, 3):
            raise ValueError("one admission operation required")
        if len(sys.argv) == 3:
            os.environ["MINDIE_AGENT_CONFIG"] = sys.argv[2]
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("admission payload must be one object")
        result = operation(sys.argv[1], payload)
        print(json.dumps(dict(ok=True, result=result), ensure_ascii=False))
    except Exception as exc:
        result = dict(ok=False, error=f"{type(exc).__name__}: {str(exc)[:240]}")
        if isinstance(exc, TranscriptSourceError):
            result["failure"] = exc.failure
        print(
            json.dumps(
                result,
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
