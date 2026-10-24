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
import time


@contextmanager
def exclusive_lock(path, *, timeout=825):
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
            except OSError as exc:
                if exc.errno not in (11, 13, 36):
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('runtime preparation lock is busy')
            time.sleep(min(.05, remaining))
        yield
    finally:
        os.close(descriptor)
