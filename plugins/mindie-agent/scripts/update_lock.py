"""Shared/exclusive non-blocking file locks with a platform boundary.

POSIX uses flock; Windows uses LockFileEx (implemented, not yet verified on
real hardware). `update_lock(config)` locks the sidecar next to the adapter
config; `file_lock(path)` locks any explicit path (updater check serialization).
"""

from contextlib import contextmanager
import os

if os.name == "posix":
    import fcntl

    @contextmanager
    def file_lock(path, *, exclusive=False):
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(
                descriptor,
                (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
            yield
        finally:
            os.close(descriptor)
else:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    LOCKFILE_FAIL_IMMEDIATELY = 0x1
    LOCKFILE_EXCLUSIVE_LOCK = 0x2

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    ]

    @contextmanager
    def file_lock(path, *, exclusive=False):
        # Windows (unverified on real hardware).
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = msvcrt.get_osfhandle(descriptor)
        overlapped = OVERLAPPED()
        flags = LOCKFILE_FAIL_IMMEDIATELY | (
            LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
        )
        try:
            if not kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
                raise BlockingIOError("MindIE update lock is held")
            try:
                yield
            finally:
                kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        finally:
            os.close(descriptor)


@contextmanager
def update_lock(config, *, exclusive=False):
    with file_lock(config.with_suffix(".update.lock"), exclusive=exclusive):
        yield
