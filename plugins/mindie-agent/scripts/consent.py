"""Install-level one-time choices: the profile consent authority, thin wrapper.

The document (``mindie-consent/1``) lives beside the adapter configuration so
every adapter sharing this profile reads the same saved choice; an isolated
profile never inherits another profile's choice.

All storage and locking semantics come directly from the shared runtime's
``mindie_knowledge.consent_store``. This module only resolves profile paths and
records explicit user choices; it never imports inferred development consent.
"""

from __future__ import annotations

import json
from pathlib import Path

from mindie_knowledge import consent_store
from session_gate import config_path

SCHEMA = consent_store.SCHEMA
CHOICES = consent_store.CHOICES
REPORTING = consent_store.REPORTING
ConsentError = consent_store.ConsentError


def consent_path_for(config_file) -> Path:
    """The consent document beside the given adapter configuration."""
    return Path(config_file).expanduser().absolute().parent / "mindie-consent.json"


def consent_path() -> Path:
    """The profile-shared consent document (sibling of the adapter config)."""
    return consent_path_for(config_path())


def shared_community_path_for(config_file) -> Path:
    """The profile-shared community settings path beside the given config."""
    return Path(config_file).expanduser().absolute().parent / "mindie-community.json"


def shared_community_path() -> Path:
    """The profile-shared community settings path."""
    return shared_community_path_for(config_path())


def load(config_file=None) -> dict:
    """Read the persistent choice. Pure: no import, creation or repair.

    Returns ``state`` of ``ok``/``missing``/``unreadable``/``corrupt`` plus
    the saved ``choice`` and ``reporting`` values when valid. An invalid
    saved choice value is corrupt, not absent.
    """
    path = consent_path_for(config_file) if config_file is not None else consent_path()
    return consent_store.read(path)


def record_choice(choice: str, config_file=None) -> str:
    path = consent_path_for(config_file) if config_file is not None else consent_path()
    consent_store.record_choice(path, choice)
    return choice


def record_reporting(value: str, config_file=None) -> str:
    path = consent_path_for(config_file) if config_file is not None else consent_path()
    consent_store.record_reporting(path, value)
    return value


def _read_adapter_config(config_file: Path) -> dict:
    """Absence is a new installation; corrupt configuration is a fault."""
    try:
        data = json.loads(Path(config_file).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConsentError("adapter configuration is unreadable", state="unreadable") from exc
    except ValueError as exc:
        raise ConsentError("adapter configuration is corrupt", state="corrupt") from exc
    if not isinstance(data, dict):
        raise ConsentError("adapter configuration is not an object", state="corrupt")
    return data








def install_traces(config_file=None) -> bool:
    """Recognize existing configuration without inferring authorization."""
    config_file = Path(config_file) if config_file is not None else config_path()
    if consent_path_for(config_file).exists():
        return True
    _read_adapter_config(config_file)
    return config_file.exists() or shared_community_path_for(config_file).exists()
