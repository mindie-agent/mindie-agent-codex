"""Shared/exclusive file locks backed by `mindie_harness.locks` with fallback.

[Role]
Provides reentrant same-mode thread-aware file locking (`file_lock` and
`update_lock`) backed by `mindie_harness.locks` (falling back to
`mindie_knowledge.loop.locks` on pre-harness runtimes).

[Invariants]
- Same-thread nested acquires of the *same* mode reenter cleanly.
- Same-thread nested acquires of the *opposite* mode raise `BlockingIOError`
  immediately to prevent in-process shared-to-exclusive deadlocks.

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
import time

_tls = threading.local()


def _acquire_os_lock(descriptor: int, *, exclusive: bool, blocking: bool) -> None:
    try:
        from mindie_harness.locks import _try_lock_fd
    except ModuleNotFoundError:
        from mindie_knowledge.loop.locks import lock_file
        lock_file(descriptor, exclusive=exclusive, blocking=blocking)
        return
    if _try_lock_fd(descriptor, exclusive=exclusive):
        return
    if not blocking:
        raise BlockingIOError("lock is held by another process")
    sleep_step = 0.01
    while not _try_lock_fd(descriptor, exclusive=exclusive):
        time.sleep(sleep_step)
        sleep_step = min(0.1, sleep_step * 1.5)


def _release_os_lock(descriptor: int) -> None:
    try:
        from mindie_harness.locks import _unlock_fd
    except ModuleNotFoundError:
        from mindie_knowledge.loop.locks import unlock_file
        unlock_file(descriptor)
        return
    _unlock_fd(descriptor)


@contextmanager
def file_lock(path, *, exclusive=False, blocking=False):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        _acquire_os_lock(descriptor, exclusive=exclusive, blocking=blocking)
        acquired = True
        yield descriptor
    finally:
        try:
            if acquired:
                _release_os_lock(descriptor)
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
