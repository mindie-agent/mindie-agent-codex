"""Thin stdlib adapter to shared diagnostics. Import performs no I/O."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path

COMPONENT = "mindie-agent-codex"
HOST = "codex"
REPOSITORY = "mindie-agent/mindie-agent"
_STORAGE = "MindIE diagnostic storage unavailable; original outcome unchanged."
_warned = False


def _hex(value, length):
    if isinstance(value, str) and len(value) == length:
        if all(char in "0123456789abcdef" for char in value):
            return value
    return None


def _read_build_json(path):
    """Optional bounded regular-file metadata; no pipe reads."""
    fd = None
    try:
        if path.is_symlink():
            return {}
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2048:
            return {}
        raw = os.read(fd, 2049)
        if len(raw) > 2048:
            return {}
        data = json.loads(raw.decode())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass


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


def _warn():
    global _warned
    if _warned:
        return
    _warned = True
    try:
        descriptor = sys.stderr.fileno()
        if not os.get_blocking(descriptor):
            os.write(descriptor, (_STORAGE + "\n").encode("ascii"))
    except Exception:
        pass


def _record():
    try:
        from mindie_diagnostics.integration import record_failure
    except ImportError:
        from diagnostic_fallback import record_failure
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
        return result
    except Exception:
        _warn()
        return {"logging_failed": True}


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
    incident = projected.get("incident_id")
    command = shlex.join([sys.executable, str(Path(__file__).with_name("bridge.py")), "reporting-status"])
    text = (f"MindIE incident {incident}; read-only local status: {command}. Upload remains a separate opt-in."
            if incident else _STORAGE)
    content = result.get("content")
    if isinstance(content, list):
        result["content"] = [*content, {"type": "text", "text": text}]
    elif "content" not in result:
        result["content"] = [{"type": "text", "text": text}]
    return result


def reporting_config_path():
    explicit = os.environ.get("MINDIE_DIAGNOSTICS_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (base / "mindie-agent" / "diagnostics.json").expanduser().resolve()


def _unavailable(stage, error_type):
    return {
        "enabled": None, "status": "unavailable",
        "error": {"stage": stage, "type": error_type},
        "hint": "Inspect the selected MindIE runtime; no repair or retry was started.",
    }


def reporting_status():
    try:
        from mindie_diagnostics.integration import reporting_status as upstream
        result = upstream(config=reporting_config_path())
        if not isinstance(result, dict):
            raise TypeError("invalid reporting status")
        return result
    except Exception as exc:
        return _unavailable("reporting_status", type(exc).__name__)


def ensure_argv(python):
    if not isinstance(python, str) or not Path(python).is_absolute():
        raise ValueError("selected runtime requires an absolute interpreter")
    return [python, "-m", "mindie_diagnostics.cli", "reporting", "ensure",
            "--config", str(reporting_config_path())]


def configure_reporting(enabled, python):
    if type(enabled) is not bool:
        return _unavailable("reporting_configure", "TypeError")
    try:
        command = ensure_argv(python)
        from mindie_diagnostics.integration import configure_reporting as upstream
        result = upstream(enabled, repository=REPOSITORY, config=reporting_config_path())
        if not isinstance(result, dict) or result.get("enabled") is not enabled:
            return _unavailable("reporting_configure", "ConfigurationUnconfirmed")
        result = dict(result, run_outside_hook=False)
        if enabled:
            result.update(
                run_outside_hook=True, command=command,
                command_line=shlex.join(command),
                note=("Reporting settings are enabled. Execute this command once outside "
                      "the Hook and inspect its result before claiming reporter readiness. "
                      "Report any failure; do not automatically retry."),
            )
        return result
    except Exception as exc:
        return _unavailable("reporting_configure", type(exc).__name__)


def reporting_hint():
    return {
        "optional": True, "recommended": True,
        "independent_of_knowledge_contribution": True,
        "enable": "scripts/bridge.py reporting-enable",
        "status": "scripts/bridge.py reporting-status",
        "disable": "scripts/bridge.py reporting-disable",
        "repository": REPOSITORY,
        "note": ("Optionally report sanitized product fault code metadata to the public "
                 "repository. Prompts, transcripts, commands, environment and credentials "
                 "are excluded. This is a separate shared user choice; consult its status."),
    }
