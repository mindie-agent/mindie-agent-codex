"""Install-level one-time choices: the single persistent consent authority.

One small JSON document (``mindie-consent/1``) per installation profile,
stored beside the adapter configuration so every adapter sharing this
profile reads the same saved choice. An independently isolated profile has
its own file and never inherits another profile's choice.

The adapter-config ``sharing_choice`` key is a one-time migration source,
never a second consent source. A missing, unreadable, corrupt and
explicitly disabled state stay strictly apart: damaged saved state is a
fault (read-only help keeps working, writes stop), never a fresh install
and never a guessed opt-in or opt-out.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from session_gate import config_path

SCHEMA = "mindie-consent/1"
CHOICES = ("contribute", "read-only", "later", "disabled")
REPORTING = ("enabled", "disabled", "later")


def consent_path() -> Path:
    """The profile-shared consent document (sibling of the adapter config)."""
    return config_path().parent / "mindie-consent.json"


def shared_community_path() -> Path:
    """The profile-shared community settings path."""
    return config_path().parent / "mindie-community.json"


def _store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read_json(path: Path):
    """(state, data): ok / missing / unreadable / corrupt. Never raises."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None
    if len(raw) > 64 * 1024:
        return "corrupt", None
    try:
        data = json.loads(raw)
    except ValueError:
        return "corrupt", None
    if not isinstance(data, dict):
        return "corrupt", None
    return "ok", data


def _legacy_config_choice():
    try:
        value = json.loads(config_path().read_text()).get("sharing_choice")
    except (OSError, ValueError):
        return None
    return value if value in CHOICES else None


def _legacy_community_choice():
    """A legacy settings file with enabled=true was an explicit public
    opt-in; anything else proves no choice (never guessed)."""
    for candidate in (shared_community_path(), _legacy_community_path()):
        if candidate is None:
            continue
        state, data = _read_json(candidate)
        if state == "ok" and data.get("enabled") is True:
            return "contribute"
    return None


def _legacy_community_path():
    try:
        value = json.loads(config_path().read_text()).get("community_config")
    except (OSError, ValueError):
        return None
    if isinstance(value, str) and os.path.isabs(value):
        return Path(value)
    return None


def _import_legacy_once(path: Path) -> None:
    choice = _legacy_config_choice() or _legacy_community_choice()
    if choice is None:
        return
    _store(path, {
        "schema": SCHEMA,
        "choice": choice,
        "choice_at": time.time(),
        "migrated_from": "legacy",
    })


def load() -> dict:
    """Read the persistent choice, importing legacy state exactly once.

    Returns ``state`` of ``ok``/``missing``/``unreadable``/``corrupt`` plus
    the saved ``choice`` and ``reporting`` values when valid. An invalid
    saved choice value is corrupt, not absent.
    """
    path = consent_path()
    if not path.exists():
        try:
            _import_legacy_once(path)
        except OSError:
            pass  # an unwritable profile stays truthful below
    state, data = _read_json(path)
    result = dict(state=state, choice=None, reporting=None, path=str(path),
                  error=None)
    if state != "ok":
        if state == "corrupt":
            result["error"] = "consent file is damaged"
        elif state == "unreadable":
            result["error"] = "consent file is unreadable"
        return result
    if data.get("schema") != SCHEMA:
        result.update(state="corrupt", error="unsupported consent schema")
        return result
    choice = data.get("choice")
    if choice is not None and choice not in CHOICES:
        result.update(state="corrupt", error="unknown consent choice")
        return result
    reporting = data.get("reporting")
    if reporting is not None and reporting not in REPORTING:
        result.update(state="corrupt", error="unknown reporting choice")
        return result
    result.update(choice=choice, reporting=reporting)
    return result


def record_choice(choice: str) -> str:
    if choice not in CHOICES:
        raise ValueError("choice must be contribute, read-only, later or disabled")
    current = load()
    data = {}
    if current["state"] == "ok":
        data = dict(_read_json(consent_path())[1])
    data.update(schema=SCHEMA, choice=choice, choice_at=time.time())
    data.pop("migrated_from", None)
    _store(consent_path(), data)
    return choice


def record_reporting(value: str) -> str:
    if value not in REPORTING:
        raise ValueError("reporting must be enabled, disabled or later")
    current = load()
    data = {}
    if current["state"] == "ok":
        data = dict(_read_json(consent_path())[1])
    data.update(schema=SCHEMA, reporting=value, reporting_at=time.time())
    _store(consent_path(), data)
    return value


def install_traces() -> bool:
    """Any evidence this installation was set up before — consent (any
    state), community settings (shared or legacy, any state) or a legacy
    adapter-config choice. A cold install has none; everything else is an
    existing installation and never re-runs first-time onboarding."""
    if consent_path().exists() or shared_community_path().exists():
        return True
    legacy = _legacy_community_path()
    if legacy is not None and legacy.exists():
        return True
    return _legacy_config_choice() is not None
