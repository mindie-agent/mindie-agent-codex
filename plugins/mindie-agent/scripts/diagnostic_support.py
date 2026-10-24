"""Thin stdlib adapter to shared diagnostics. Import performs no I/O.

[Role]
Bridges plugin faults, pending incident projection, and unified contribution
consent to the shared diagnostics runtime.

[Invariants]
- The shared runtime owns incident storage; unavailable storage is reported without a second recorder.
- Never includes prompts, transcripts, commands, environment variables, or credentials in diagnostic records.
- Saved consent choice in `consent.py` takes precedence over stale reporter policy (`effective_reporting`).

[Timeouts & Locks]
- Fault recording uses bounded local reads and SQLite projection. Explicit
  reporter activation has a 75-second shared budget within a 90-second caller.

[Side-Effect Policy]
- Importing this module performs no I/O; `failure()` writes local diagnostic events and enqueues a bounded projection reference.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

COMPONENT = "mindie-agent-codex"
HOST = "codex"
REPOSITORY = "mindie-agent/mindie-agent"
_STORAGE = "MindIE diagnostic storage unavailable; original outcome unchanged."
CONTEXT_ENV = ("MINDIE_DIAGNOSTICS_CONFIG", "MINDIE_DIAGNOSTICS_ROOT")


def environment_context(adapter, config_file=None):
    """Only explicit diagnostic paths belong to an installation, never credentials."""
    if not isinstance(adapter, dict):
        raise ValueError("invalid installation diagnostics configuration")
    context = adapter.get("diagnostics_context", {})
    if not isinstance(context, dict) or set(context) - set(CONTEXT_ENV):
        raise ValueError("invalid installation diagnostics context")
    for value in context.values():
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError("installation diagnostic paths must be absolute")
    context = dict(context)
    if config_file is not None:
        context.setdefault("MINDIE_DIAGNOSTICS_CONFIG",
                           str(Path(os.environ.get("MINDIE_DIAGNOSTICS_CONFIG")
                                    or Path(config_file).expanduser().absolute().parent / "diagnostics.json")
                               .expanduser().absolute()))
    return context


def capture_environment_context(environ=None):
    """Capture supported overrides only at explicit setup/enable boundaries."""
    environ = os.environ if environ is None else environ
    return {
        name: str(Path(environ[name]).expanduser().absolute())
        for name in CONTEXT_ENV
        if environ.get(name)
    }


def apply_environment_context(adapter, environ=None):
    environ = os.environ if environ is None else environ
    environ.update(environment_context(adapter))
    return environ


def read_environment_context(config):
    """Read only the existing bounded adapter document; absence adds no default."""
    path = Path(config)
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError("invalid installation diagnostics configuration")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("invalid installation diagnostics configuration")
    return environment_context(json.loads(raw.decode("utf-8")), path)


def _hex(value, length):
    if isinstance(value, str) and len(value) == length:
        if all(char in "0123456789abcdef" for char in value):
            return value
    return None


def _read_build_json(path):
    """Optional bounded regular-file metadata."""
    try:
        path = Path(path)
        if not path.is_file():
            return {}
        with path.open("rb") as stream:
            raw = stream.read(2049)
        if len(raw) > 2048:
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except TimeoutError:
        raise
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def _project_build(data):
    revision = _hex(data.get("revision"), 40)
    version = data.get("version")
    result = {"revision": revision} if revision else {}
    if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,79}", version):
        result["version"] = version
    return result


def build_metadata():
    """Use this loaded package's stamp or its existing installation receipt.

    A previous updater can install new scripts without knowing about the new
    stamp. Its manifest and prepared receipt already identify that exact
    package. Never consult the mutable current-generation pointer or infer a
    commit from the directory name alone. No writes, subprocess or network.
    """
    scripts = Path(os.path.abspath(__file__)).parent
    result = _project_build(_read_build_json(scripts / "diagnostic-build.json"))
    if "revision" in result and "version" in result:
        return result
    plugin = scripts.parent
    manifest = _read_build_json(plugin / ".codex-plugin" / "plugin.json")
    if manifest.get("name") != "mindie-agent":
        return result
    version = _project_build(manifest).get("version")
    if not version or result.get("version", version) != version:
        return result
    result.setdefault("version", version)
    receipt = _read_build_json(plugin.parent / "prepared.json")
    revision = _hex(receipt.get("revision"), 40)
    if (revision and receipt.get("plugin") == str(plugin)
            and receipt.get("version") == version and plugin.parent.name == revision):
        result.setdefault("revision", revision)
    return result


def _record():
    from mindie_diagnostics.integration import record_failure
    os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(reporting_config_path())
    return record_failure


def failure(operation, stage, category, *, exception=None, revision=None,
            elapsed_ms=None, exit_code=None, reportable=True, incident_id=None):
    """Only static product metadata; a trusted inner incident is not recorded twice."""
    trusted = _hex(incident_id, 32)
    if trusted:
        return {"incident_id": trusted, "logging_failed": False}
    inner = reference({"diagnostic": getattr(exception, "mindie_diagnostic", None)})
    if inner is not None:
        return inner
    build = build_metadata()
    explicit = _hex(revision, 40)
    try:
        raw = _record()(
            COMPONENT, operation, stage=stage, category=category,
            exception=exception, host=HOST,
            revision=explicit or build.get("revision"),
            version=build.get("version"),
            elapsed_ms=elapsed_ms, exit_code=exit_code, reportable=reportable,
        )
        if not isinstance(raw, dict):
            raise TypeError("invalid diagnostic result")
        if type(raw.get("recorded")) is not bool or type(raw.get("logging_failed")) is not bool:
            raise TypeError("invalid diagnostic result")
        result = {"recorded": raw["recorded"], "logging_failed": raw["logging_failed"]}
        incident = _hex(raw.get("incident_id"), 32)
        if incident:
            result["incident_id"] = incident
        try:
            from agent_diagnostics import enqueue
            enqueue(COMPONENT, operation, stage, category, result)
        except Exception:
            result['delivery_failed'] = True
        return result
    except Exception:
        print(_STORAGE, file=sys.stderr)
        result = {"logging_failed": True}
        try:
            from agent_diagnostics import enqueue
            enqueue(COMPONENT, operation, stage, category, result)
        except Exception:
            result['delivery_failed'] = True
        return result


def _ref(diagnostic):
    if not isinstance(diagnostic, dict):
        return None
    failed = diagnostic.get("logging_failed", False)
    if type(failed) is not bool:
        return None
    incident = _hex(diagnostic.get("incident_id"), 32)
    if incident:
        return {"incident_id": incident, "logging_failed": failed}
    if failed is True:
        return {"logging_failed": True}
    return None


def reference(result):
    """Inspect only structured references returned by a trusted inner boundary."""
    if not isinstance(result, dict):
        return None
    found = _ref(result.get("diagnostic"))
    if found is not None:
        return found
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return _ref(structured.get("diagnostic"))
    return None


def attach(result, diagnostic):
    """Preserve result semantics; expose only the projected incident reference."""
    if not isinstance(result, dict) or reference(result) is not None:
        return result
    projected = _ref(diagnostic)
    if projected is None:
        return result
    result = dict(result, diagnostic=projected)
    return result


def attach_pending(result):
    """Attach machine diagnostics to one naturally occurring capability call."""
    if not isinstance(result, dict):
        return result
    try:
        from agent_diagnostics import pending
        projection = pending()
    except Exception:
        projection = dict(items=[dict(code='diagnostic_delivery_unavailable')], remaining=None)
    if projection is None:
        return result
    existing = result.get('structuredContent')
    if existing is not None and not isinstance(existing, dict):
        return result  # Keep the pending incident; do not mask a malformed result.
    structured = dict(existing or {})
    structured['agent_diagnostics'] = projection
    return dict(result, structuredContent=structured)


def acknowledge_pending(result):
    try:
        from agent_diagnostics import acknowledge
        acknowledge((result.get('structuredContent') or {}).get('agent_diagnostics'))
    except Exception:
        # A failed acknowledgment remains pending; the business result has
        # already been written and must never be replayed to repair it.
        pass


def reporting_config_path(config_file=None):
    explicit = os.environ.get("MINDIE_DIAGNOSTICS_CONFIG")
    if explicit:
        return Path(explicit).expanduser().absolute()
    from session_gate import config_path
    profile = Path(config_file) if config_file is not None else config_path()
    saved = read_environment_context(profile)
    return Path(saved.get("MINDIE_DIAGNOSTICS_CONFIG", profile.parent / "diagnostics.json"))


def _unavailable(stage, error_type):
    return {
        "enabled": None, "status": "unavailable",
        "error": {"stage": stage, "type": error_type},
        "hint": "Inspect the selected MindIE runtime; no repair or retry was started.",
    }


def reporting_status():
    try:
        from mindie_diagnostics.integration import reporting_status as upstream
        import consent
        result = upstream(config=reporting_config_path())
        return effective_reporting(result, consent.load())
    except Exception as exc:
        return _unavailable("reporting_status", type(exc).__name__)


def activate_reporting(config_file=None, *, ensure=True):
    """Derive policy from the existing authority; only long-lived callers ensure."""
    from mindie_knowledge.consent_store import activate_reporting as activate
    import consent
    policy = reporting_config_path(config_file)
    authority = consent.consent_path_for(config_file) if config_file is not None else consent.consent_path()
    return activate(authority, config=policy, repository=REPOSITORY, ensure=ensure)


def ensure_command_line(command):
    if os.name == "nt":
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in command)
    return shlex.join(command)


def effective_reporting(view, saved):
    """The authority gates status too; a legacy choice cannot widen consent."""
    if saved.get("state") in {"corrupt", "unreadable"}:
        return dict(view, enabled=False, status="unavailable", consent=saved)
    if not saved.get("choice_revision"):
        return dict(view, enabled=False, status="needs_choice", consent=saved,
                    detail="The saved choice does not authorize sanitized fault reporting.")
    if saved.get("choice") != "contribute":
        return dict(view, enabled=False, status="disabled", consent=saved)
    return dict(view, consent=saved)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Activate fault reporting from the saved contribution choice.")
    parser.add_argument("operation", choices=["activate", "status"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    from session_gate import bind_explicit_config
    bind_explicit_config(args.config)
    try:
        result = reporting_status() if args.operation == "status" else activate_reporting()
    except Exception as exc:
        result = _unavailable("reporting_activation", type(exc).__name__)
    print(json.dumps(result))
    return int(result.get("status") not in {"running", "disabled"}
               if args.operation == "activate" else result.get("status") == "unavailable")


if __name__ == "__main__":
    raise SystemExit(main())
