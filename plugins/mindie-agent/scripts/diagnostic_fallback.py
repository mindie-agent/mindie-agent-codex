"""Standalone local diagnostics fallback. Stdlib only."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_POLICY_SCHEMA = "mindie.diagnostics.reporting.v1"
_POLICY_PURPOSE = "tool_fault_reporting"
_MAX_EVENT_BYTES = 16384
_MAX_FILE_BYTES = 1024 * 1024
_LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,119}\Z")
_INCIDENT_RE = re.compile(r"[0-9a-f]{32}\Z")
_REVISION_RE = re.compile(r"[0-9a-f]{7,64}\Z")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}\Z")
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,119}\Z")
_NEVER_REPORTABLE = frozenset({
    "caller", "cancelled", "business", "connectivity",
    "credentials", "permission", "disabled", "prerequisite",
})
_CONSENT_KEYS = frozenset({"config_file", "revision", "repository", "purpose"})

_lock = threading.Lock()
_process_pid = None
_process_uuid = None


def _private_mkdir(path, *, parents=False, exist_ok=False):
    Path(path).mkdir(mode=0o700, parents=parents, exist_ok=exist_ok)


def policy_path(config=None) -> Path:
    if config is not None:
        return Path(config)
    env = os.environ.get("MINDIE_DIAGNOSTICS_CONFIG")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "mindie-agent" / "diagnostics.json"
    return Path.home() / ".config" / "mindie-agent" / "diagnostics.json"


def default_root() -> Path:
    env = os.environ.get("MINDIE_DIAGNOSTICS_ROOT")
    if env:
        return Path(env)
    if os.name == "nt" and not os.environ.get("XDG_STATE_HOME"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "mindie" / "diagnostics"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "mindie" / "diagnostics"
    return Path.home() / ".local" / "state" / "mindie" / "diagnostics"


def state_path(config=None) -> Path:
    return policy_path(config).with_suffix(".state")


class PolicyUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("reporting_policy_unavailable")


def read_policy(config=None):
    path = policy_path(config)
    if not path.is_absolute():
        raise PolicyUnavailable()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PolicyUnavailable() from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PolicyUnavailable() from exc
    if (not isinstance(data, dict)
            or data.get("schema") != _POLICY_SCHEMA
            or data.get("purpose") != _POLICY_PURPOSE
            or data.get("decision") not in ("enabled", "disabled")
            or not isinstance(data.get("repository"), str)
            or _REPO_RE.fullmatch(data["repository"]) is None
            or not isinstance(data.get("revision"), str)
            or _INCIDENT_RE.fullmatch(data["revision"]) is None
            or not isinstance(data.get("roots"), list)):
        raise PolicyUnavailable()
    return {
        "schema": _POLICY_SCHEMA,
        "purpose": _POLICY_PURPOSE,
        "decision": data["decision"],
        "repository": data["repository"],
        "revision": data["revision"],
        "roots": list(data["roots"]),
    }


def current_consent(config=None):
    try:
        policy = read_policy(config)
        if not policy or policy.get("decision") != "enabled":
            return None
        return {
            "config_file": str(policy_path(config)),
            "revision": policy["revision"],
            "repository": policy["repository"],
            "purpose": policy["purpose"],
        }
    except Exception:
        return None


def consent_status(reference):
    try:
        if not isinstance(reference, dict) or set(reference.keys()) != _CONSENT_KEYS:
            return "withdrawn"
        policy = read_policy(reference["config_file"])
        if policy is None:
            return "unavailable"
        matches = (
            policy["decision"] == "enabled"
            and policy["purpose"] == reference["purpose"]
            and policy["repository"] == reference["repository"]
            and policy["revision"] == reference["revision"]
        )
        return "allowed" if matches else "withdrawn"
    except PolicyUnavailable:
        return "unavailable"
    except Exception:
        return "withdrawn"


def consent_allowed(reference) -> bool:
    return consent_status(reference) == "allowed"


def scope_key(public_fingerprint, reference) -> str:
    try:
        if not isinstance(public_fingerprint, str) or not public_fingerprint:
            return ""
        if not isinstance(reference, dict) or set(reference.keys()) != _CONSENT_KEYS:
            return ""
        canonical = json.dumps(
            [reference["purpose"], reference["repository"], reference["revision"], public_fingerprint],
            ensure_ascii=True, separators=(",", ":"), allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _label(value, limit) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and _LABEL_RE.fullmatch(value) is not None


def _process_instance_id_locked() -> str:
    global _process_pid, _process_uuid
    pid = os.getpid()
    if _process_uuid is None or _process_pid != pid:
        _process_uuid = uuid.uuid4().hex
        _process_pid = pid
    return _process_uuid


def record_failure(
    component,
    operation,
    *,
    stage,
    category,
    exception=None,
    host=None,
    revision=None,
    version=None,
    elapsed_ms=None,
    exit_code=None,
    reportable=True,
    root=None,
    incident_id=None,
) -> dict:
    try:
        if isinstance(incident_id, str) and _INCIDENT_RE.fullmatch(incident_id):
            return {"recorded": True, "incident_id": incident_id, "logging_failed": False}
        if (not _label(component, 80) or not _label(operation, 120)
                or not _label(stage, 120) or not _label(category, 120)
                or not isinstance(reportable, bool)):
            return {"recorded": False, "incident_id": None, "logging_failed": False, "error": "invalid_arguments"}
        attributes = {"stage": stage, "category": category}
        if exception is not None:
            error_type = type(exception).__name__
            attributes["error_type"] = error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "Exception"
        if host is not None:
            if not _label(host, 120):
                return {"recorded": False, "incident_id": None, "logging_failed": False, "error": "invalid_arguments"}
            attributes["host"] = host
        if exit_code is not None:
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                return {"recorded": False, "incident_id": None, "logging_failed": False, "error": "invalid_arguments"}
            attributes["exit_code"] = exit_code
        fresh = uuid.uuid4().hex
        effective = False if category in _NEVER_REPORTABLE else reportable
        event = {
            "schema": 1,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "monotonic_ns": time.monotonic_ns(),
            "pid": os.getpid(),
            "component": component,
            "severity": "ERROR",
            "event": "operation.end",
            "operation_id": fresh,
            "trace_id": fresh,
            "operation": operation,
            "status": "error",
            "attributes": attributes,
            "reportable": effective,
            "reporting": current_consent() if effective else None,
        }
        if elapsed_ms is not None and not isinstance(elapsed_ms, bool) and isinstance(elapsed_ms, (int, float)) and math.isfinite(elapsed_ms) and elapsed_ms >= 0:
            event["duration_ms"] = elapsed_ms
        if isinstance(revision, str) and _REVISION_RE.fullmatch(revision):
            event["package_revision"] = revision
        if isinstance(version, str) and _VERSION_RE.fullmatch(version):
            event["package_version"] = version
        with _lock:
            base = Path(default_root() if root is None else root)
            leaf = base / "events" / component
            leaf.mkdir(mode=0o700, parents=True, exist_ok=True)
            proc = _process_instance_id_locked()
            event["process_instance_id"] = proc
            line = (json.dumps(event, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            if len(line) > _MAX_EVENT_BYTES:
                return {"recorded": False, "incident_id": None, "logging_failed": True, "error": "logging_failed"}
            target = leaf / f"{os.getpid()}-{proc}.jsonl"
            with target.open("ab") as stream:
                stream.write(line)
        return {"recorded": True, "incident_id": fresh, "logging_failed": False}
    except Exception:
        return {"recorded": False, "incident_id": None, "logging_failed": True, "error": "logging_failed"}
