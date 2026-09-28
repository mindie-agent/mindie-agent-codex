"""Install-level one-time choices: the profile consent authority, thin wrapper.

The document (``mindie-consent/1``) lives beside the adapter configuration so
every adapter sharing this profile reads the same saved choice; an isolated
profile never inherits another profile's choice.

All storage semantics come from the shared core consent store — this adapter
carries it as the byte-identical bootstrap copy ``consent_store.py`` next to
this script (canonical source: ``mindie_knowledge/consent_store.py`` in the
knowledge repository; source commit and SHA-256 are pinned in the lane's
CANDIDATE.json and checked for equality at integration). This module only
adds the adapter's path resolution and its legacy host sources:

- ``load`` is a pure read: never creates, imports or repairs anything.
- ``record_choice``/``record_reporting`` are explicit user-intent updates.
- ``migrate_legacy`` runs only at the explicit install/upgrade/entry-attach
  boundaries: it collects already validated legacy records (the retired
  adapter-config ``sharing_choice`` key; a legacy community settings file
  with enabled=true) and delegates the one-time import to the store. Damaged
  legacy evidence is a diagnosable error that preserves the original data —
  never a guessed public authorization.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import consent_store
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
    """One read of the adapter configuration; {} when absent or invalid."""
    try:
        data = json.loads(Path(config_file).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_community_files(config_file: Path, config: dict):
    """Candidate legacy community files: the shared path and the configured
    legacy pointer, deduplicated by resolved path."""
    seen = set()
    paths = [shared_community_path_for(config_file)]
    value = config.get("community_config")
    if isinstance(value, str) and os.path.isabs(value):
        paths.append(Path(value))
    result = []
    for candidate in paths:
        key = str(candidate.expanduser().absolute())
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def legacy_candidates(config_file=None) -> dict:
    """Validated pre-authority choice records plus per-source problems.

    A legacy settings file with enabled=true was an explicit public opt-in;
    anything else proves no choice (never guessed). A damaged or unreadable
    legacy source is a diagnosable problem, not consent evidence.
    """
    config_file = Path(config_file) if config_file is not None else config_path()
    config = _read_adapter_config(config_file)
    candidates = []
    problems = []
    choice = config.get("sharing_choice")
    if choice in CHOICES:
        candidates.append(
            dict(choice=choice, reporting=None, source="adapter-config")
        )
    for path in _legacy_community_files(config_file, config):
        try:
            raw = consent_store._read_bytes(path)
        except FileNotFoundError:
            continue
        except OSError:
            problems.append(dict(source=str(path), state="unreadable"))
            continue
        try:
            data = json.loads(raw) if len(raw) <= consent_store.MAX_BYTES else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            problems.append(dict(source=str(path), state="corrupt"))
            continue
        if data.get("enabled") is True:
            candidates.append(
                dict(choice="contribute", reporting=None,
                     source=f"community-enabled:{path}")
            )
    return dict(candidates=candidates, problems=problems)


def migrate_legacy(config_file=None) -> dict:
    """One-time import of validated legacy choices at an explicit boundary.

    An existing valid authority always wins (``kept``). Damaged legacy
    sources are an ``error`` that writes nothing; otherwise the shared
    store's ``migrate`` decides (``migrated``/``absent``/``conflict``
    /``error``) under the cross-process lock. A no-evidence call creates
    no state at all.
    """
    config_file = Path(config_file) if config_file is not None else config_path()
    path = consent_path_for(config_file)
    view = consent_store.read(path)
    if view["state"] == "ok" and view["choice"] in CHOICES:
        return dict(
            status="kept", choice=view["choice"],
            reporting=view["reporting"], path=str(path),
        )
    if view["state"] in {"corrupt", "unreadable"}:
        return dict(
            status="error", state=view["state"], error=view["error"],
            path=str(path),
        )
    found = legacy_candidates(config_file)
    if found["problems"]:
        return dict(
            status="error",
            state="damaged-legacy",
            error="legacy consent evidence is unreadable or damaged",
            problems=found["problems"],
            path=str(path),
        )
    if not found["candidates"]:
        return dict(status="absent", state=view["state"], path=str(path))
    return consent_store.migrate(path, found["candidates"])


def install_traces(config_file=None) -> bool:
    """Any evidence this installation was set up before — consent (any
    state), community settings (shared or legacy, any state) or a legacy
    adapter-config choice. A cold install has none; everything else is an
    existing installation and never re-runs first-time onboarding."""
    config_file = Path(config_file) if config_file is not None else config_path()
    if consent_path_for(config_file).exists():
        return True
    config = _read_adapter_config(config_file)
    for path in _legacy_community_files(config_file, config):
        if path.exists():
            return True
    return config.get("sharing_choice") in CHOICES
