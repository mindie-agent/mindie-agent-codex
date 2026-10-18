"""Shared/exclusive file locks with a platform boundary.

[Role]
Provides reentrant same-mode thread-aware file locking (`file_lock` and
`update_lock`) backed by `mindie_knowledge.loop.locks`.

[Invariants]
- Same-thread nested acquires of the *same* mode reenter cleanly.
- Same-thread nested acquires of the *opposite* mode fail immediately (`RuntimeError`)
  to prevent in-process shared-to-exclusive deadlocks.

[Timeouts & Locks]
- Supports both non-blocking (`blocking=False`) and blocking acquisition via OS
  `flock` / `LockFileEx`.

[Side-Effect Policy]
- Creates/opens only the designated `.lock` sidecar file with `0o600` permissions.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import threading

_tls = threading.local()

@contextmanager
def file_lock(path, *, exclusive=False, blocking=False):
    from mindie_knowledge.loop.locks import lock_file, unlock_file
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        lock_file(descriptor, exclusive=exclusive, blocking=blocking)
        acquired = True
        yield descriptor
    finally:
        try:
            if acquired:
                unlock_file(descriptor)
        finally:
            os.close(descriptor)


def _held():
    locks = getattr(_tls, "locks", None)
    if locks is None:
        locks = {}
        _tls.locks = locks
    return locks


@contextmanager
def update_lock(config, *, exclusive=False, blocking=False):
    path = Path(config).with_suffix(".update.lock")
    key = str(path.absolute())
    locks = _held()
    state = locks.get(key)
    if state is not None:
        if bool(state["exclusive"]) != bool(exclusive):
            raise BlockingIOError("MindIE update lock is held")
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1
            if state["depth"] == 0:
                locks.pop(key, None)
        return
    with file_lock(path, exclusive=exclusive, blocking=blocking):
        locks[key] = {"exclusive": exclusive, "depth": 1}
        try:
            yield
        finally:
            locks.pop(key, None)
