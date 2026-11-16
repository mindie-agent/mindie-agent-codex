"""[Role] Owner-lock sidecars that prove which process owns remote receipts.
[Invariants]
- A sidecar is `<32 lowercase hex>.owner.lock`; it is advisory liveness
  evidence, never authoritative receipt state.
- A sidecar is removed only after a non-blocking exclusive OS lock proves no
  process holds it. Held, unreadable or racing files are always kept.
- Owner identities are unique UUIDs; reusing another owner's path is refused
  before any scan or deletion.
- A failed unlink is a warning, never a fault that changes a served request.
[Timeouts & Locks]
- One non-blocking lock probe per candidate; scans stop at 256 entries and a
  1.0s wall-clock budget. `TimeoutError` from the filesystem always propagates.
[Side-Effect Policy]
- Creates/removes only `<owner>.owner.lock` sidecars in the caller's receipt
  directory; never the receipt database, layout metadata or user state.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import time

from update_lock import file_lock

OWNER_SUFFIX = ".owner.lock"
SWEEP_MIN_AGE_SECONDS = 60.0
SWEEP_BUDGET_SECONDS = 1.0
MAX_SWEEP_ENTRIES = 256
_OWNER = re.compile(r"[0-9a-f]{32}\Z")


def owner_path(directory, owner):
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise ValueError("remote receipt owner identity is invalid")
    return Path(directory) / (owner + OWNER_SUFFIX)


def owner_is_alive(path):
    """True when another process holds ``path``; a missing file is not alive."""
    path = Path(path)
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except TimeoutError:
        raise
    except OSError:
        return True
    try:
        try:
            from mindie_knowledge.loop.locks import lock_file
            lock_file(descriptor, exclusive=True)
        except BlockingIOError:
            return True
        except TimeoutError:
            raise
        except OSError:
            return True
        try:
            from mindie_knowledge.loop.locks import unlock_file
            unlock_file(descriptor)
        except TimeoutError:
            raise
        except OSError:
            pass
        return False
    finally:
        os.close(descriptor)


def discard_owner_lock(path):
    """Remove one idle sidecar; return True only when this call unlinked it."""
    path = Path(path)
    if os.name == "nt":
        # NTFS rejects unlink while any handle is open, so probe first and
        # close before unlinking. The owner id is unique, so the close/unlink
        # window cannot hand this sidecar to a different logical owner.
        if owner_is_alive(path):
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except TimeoutError:
            raise
        except OSError:
            return False
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except TimeoutError:
        raise
    except OSError:
        return False
    locked = False
    try:
        try:
            from mindie_knowledge.loop.locks import lock_file
            lock_file(descriptor, exclusive=True)
            locked = True
        except BlockingIOError:
            return False
        except TimeoutError:
            raise
        except OSError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except TimeoutError:
            raise
        except OSError:
            return False
    finally:
        try:
            if locked:
                from mindie_knowledge.loop.locks import unlock_file
                unlock_file(descriptor)
        except TimeoutError:
            raise
        except OSError:
            pass
        finally:
            os.close(descriptor)


@contextmanager
def owned_lock(path):
    """Hold an owner sidecar for the context, then remove it when idle."""
    path = Path(path)
    with file_lock(path, exclusive=True):
        yield
    discard_owner_lock(path)


def sweep_idle_owner_locks(directory, *, keep=(), min_age=SWEEP_MIN_AGE_SECONDS,
                           deadline=None):
    """Remove historical, unlocked owner sidecars within one bounded scan."""
    directory = Path(directory)
    limit = time.monotonic() + SWEEP_BUDGET_SECONDS
    if deadline is not None:
        limit = min(limit, deadline)
    keep_owners = {str(value) for value in keep}
    removed = []
    scanned = 0
    try:
        with os.scandir(directory) as iterator:
            for item in iterator:
                if time.monotonic() >= limit or scanned >= MAX_SWEEP_ENTRIES:
                    break
                scanned += 1
                name = item.name
                if not name.endswith(OWNER_SUFFIX):
                    continue
                owner = name[:-len(OWNER_SUFFIX)]
                if not _OWNER.fullmatch(owner) or owner in keep_owners:
                    continue
                try:
                    modified = item.stat().st_mtime
                except FileNotFoundError:
                    continue
                except TimeoutError:
                    raise
                except OSError:
                    continue
                if min_age > 0 and time.time() - modified < min_age:
                    continue
                if discard_owner_lock(directory / name):
                    removed.append(name)
    except FileNotFoundError:
        return removed
    except TimeoutError:
        raise
    except OSError:
        return removed
    return removed


__all__ = [
    "MAX_SWEEP_ENTRIES",
    "OWNER_SUFFIX",
    "SWEEP_MIN_AGE_SECONDS",
    "discard_owner_lock",
    "owned_lock",
    "owner_is_alive",
    "owner_path",
    "sweep_idle_owner_locks",
]
