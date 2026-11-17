"""Exclusive uv-publication lock delegating to `mindie_harness.locks` when available.

[Role]
Provides a blocking exclusive file lock (`exclusive_lock(path)`) used by
`bootstrap.py` before and after `mindie_harness` is installed.

[Invariants]
- Delegates to `mindie_harness.locks.FileLock` when `mindie_harness` is installed;
  falls back to stdlib `fcntl.flock` / `msvcrt.locking` during cold bootstrap.

[Timeouts & Locks]
- Blocking exclusive lock held up to `timeout` (`825s` default).

[Side-Effect Policy]
- Opens/creates the `.lock` file with mode `0o600` and releases on context exit.
"""
from contextlib import contextmanager
import errno
import os
import time

_LOCK_CONTENTION = frozenset((
    errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK,
))


@contextmanager
def exclusive_lock(path, *, timeout=825):
    try:
        from mindie_harness.locks import FileLock
    except ModuleNotFoundError:
        FileLock = None
    if FileLock is not None:
        lock = FileLock(path, exclusive=True, timeout=timeout)
        try:
            lock.acquire()
        except TimeoutError as exc:
            raise TimeoutError("runtime preparation lock is busy") from exc
        try:
            yield
        finally:
            lock.release()
        return

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except TimeoutError:
                raise
            except OSError as exc:
                if exc.errno not in _LOCK_CONTENTION:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("runtime preparation lock is busy")
            time.sleep(min(.05, remaining))
        yield
    finally:
        os.close(descriptor)
