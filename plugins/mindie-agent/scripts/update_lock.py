"""Coordinate explicit activation and bounded calls with an idle-only upgrade."""

from contextlib import contextmanager
import fcntl
import os


@contextmanager
def update_lock(config, *, exclusive=False):
    path = config.with_suffix(".update.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(
            descriptor,
            (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
        )
        yield
    finally:
        os.close(descriptor)
