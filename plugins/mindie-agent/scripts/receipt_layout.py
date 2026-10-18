"""Remote receipt directory layout and format migration guard.

[Role]
Manages versioned directory layout (`gate-v<FORMAT>`) and `gate-layout.json` metadata for remote MCP request receipts.

[Invariants]
- Remote receipt formats may reset during development (`RELEASE_VERSION=None`), never silently after a product release (`RELEASE_VERSION` set).
- Missing `gate-layout.json` when `gate-v*/*.sqlite3` already exists fails closed to preserve existing receipts.

[Timeouts & Locks]
- Uses blocking `file_lock` on `gate-layout.lock` during `prepare_layout()`.

[Side-Effect Policy]
- `receipt_root()` is read-only; `prepare_layout()` atomically writes `gate-layout.json` via `os.replace()` only after the caller's block succeeds.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile

from update_lock import file_lock

FORMAT = 1
# The first product release protects remote effects, including uncertain writes.
RELEASE_VERSION = '0.0.1'


def _version(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value)):
        raise ValueError('remote receipt release version must be normal or null during development')
    return value


def _read(base):
    path = base / 'gate-layout.json'
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('remote receipt layout must be a regular file')
    with path.open('rb') as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError('remote receipt layout exceeds its metadata bound')
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'schema', 'format', 'release_version'}
            or value['schema'] != 'mindie-remote-layout/1'
            or type(value['format']) is not int or value['format'] < 1):
        raise ValueError('remote receipt layout is invalid; existing receipts were preserved')
    _version(value['release_version'])
    return value


def receipt_root(base):
    """Read-only compatibility check; a released format needs migration."""
    base = Path(base)
    previous = _read(base)
    _version(RELEASE_VERSION)
    if previous is None and any(path for directory in base.glob('gate-v[0-9]*')
                                for path in directory.glob('*.sqlite3')):
        raise ValueError('remote receipt layout is missing; existing receipts were preserved')
    if previous is not None:
        if not (base / ('gate-v' + str(previous['format']))).is_dir():
            raise ValueError('initialized remote receipt directory is missing; layout was preserved')
        if previous['format'] != FORMAT and previous['release_version'] is not None:
            raise ValueError('released remote receipts require an explicit format migration; receipts were preserved')
    return base / ('gate-v' + str(FORMAT))


@contextmanager
def prepare_layout(base):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    with file_lock(base / 'gate-layout.lock', exclusive=True, blocking=True):
        selected = receipt_root(base)
        previous = _read(base)
        yield selected
        release = RELEASE_VERSION
        if previous is not None and previous['format'] == FORMAT:
            release = release or previous['release_version']
        value = dict(schema='mindie-remote-layout/1', format=FORMAT, release_version=release)
        if value != previous:
            descriptor, name = tempfile.mkstemp(dir=base, prefix='.gate-layout-')
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(value, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, base / 'gate-layout.json')
            except BaseException as error:
                try:
                    Path(name).unlink(missing_ok=True)
                except OSError as cleanup:
                    error.add_note('Layout cleanup also failed: ' + type(cleanup).__name__)
                raise
            else:
                Path(name).unlink(missing_ok=True)
