"""Install-level one-time choices: the profile consent authority, thin wrapper.

[Role]
Wraps `mindie_knowledge.consent_store` (`load`, `record_choice`, `record_reporting`, `install_traces`) for the active Codex profile (`mindie-consent.json`).

[Invariants]
- Community contribution and issue reporting choices are independent install-level records (`SCHEMA`, `CHOICES`, `REPORTING`).

[Timeouts & Locks]
- Delegates atomic consent file locking and writes to `mindie_knowledge.consent_store`.

[Side-Effect Policy]
- `load()` and `install_traces()` are read-only; `record_choice()` and `record_reporting()` atomically update `mindie-consent.json`.
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
    return Path(config_file).expanduser().absolute().parent / "mindie-consent.json"


def consent_path() -> Path:
    return consent_path_for(config_path())


def shared_community_path_for(config_file) -> Path:
    return Path(config_file).expanduser().absolute().parent / "mindie-community.json"


def shared_community_path() -> Path:
    return shared_community_path_for(config_path())


def load(config_file=None) -> dict:
    path = consent_path_for(config_file) if config_file is not None else consent_path()
    return consent_store.read(path)


def record_choice(choice: str, config_file=None) -> str:
    path = consent_path_for(config_file) if config_file is not None else consent_path()
    try:
        consent_store.record_choice(path, choice)
    except consent_store.ConsentError as exc:
        if exc.state == "corrupt":
            path.unlink(missing_ok=True)
            consent_store.record_choice(path, choice)
        else:
            raise
    return choice


def record_reporting(value: str, config_file=None) -> str:
    path = consent_path_for(config_file) if config_file is not None else consent_path()
    try:
        consent_store.record_reporting(path, value)
    except consent_store.ConsentError as exc:
        if exc.state == "corrupt":
            path.unlink(missing_ok=True)
            consent_store.record_reporting(path, value)
        else:
            raise
    return value


def _read_adapter_config(config_file: Path) -> dict:
    try:
        data = json.loads(Path(config_file).read_text(encoding="utf-8"))
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
    config_file = Path(config_file) if config_file is not None else config_path()
    if consent_path_for(config_file).exists():
        return True
    _read_adapter_config(config_file)
    return config_file.exists() or shared_community_path_for(config_file).exists()
