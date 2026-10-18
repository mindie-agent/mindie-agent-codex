"""Exclusive uv-publication lock before the shared runtime is installed.

[Role]
Provides a stdlib-only blocking exclusive file lock (`exclusive_lock(path)`) used by `bootstrap.py` before `mindie_knowledge` is installed.

[Invariants]
- Uses POSIX `fcntl.flock(LOCK_EX)` and Windows `msvcrt.locking` on byte 0.

[Timeouts & Locks]
- Blocking exclusive lock held for the duration of `ensure_runtime` preparation.

[Side-Effect Policy]
- Opens/creates the lock file with mode `0o600` and releases the lock on context exit.
"""
from contextlib import contextmanager
import os


@contextmanager
def exclusive_lock(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            # LockFileEx's runtime lock and this bootstrap lock use byte zero.
            # LK_LOCK has an implicit ten-retry limit. Poll the same byte-range
            # lock until the other bootstrap releases it; no new lock authority.
            import time
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (13, 36):
                        raise
                    time.sleep(.05)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
