"""Stdlib-only Windows process-tree ownership for bounded subprocesses.

The process starts suspended so it cannot create descendants before it joins
the Job. Closing the Job kills every owned process, even when its leader has
already exited and a descendant still holds an inherited pipe.
"""

import ctypes
import os
import subprocess
from ctypes import wintypes


_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


def spawn(command, *, allow_service=False, **kwargs):
    """Create a suspended child, assign its Job, then resume its first thread."""
    if os.name != "nt":
        return subprocess.Popen(command, **kwargs)

    kwargs["creationflags"] = (
        kwargs.get("creationflags", 0)
        | subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_NO_WINDOW
        | _CREATE_SUSPENDED
    )
    process = subprocess.Popen(command, **kwargs)
    try:
        _assign_job(process, allow_service=allow_service)
        _resume_windows_process(process)
    except BaseException:
        # The suspended process has no descendants unless it was successfully
        # resumed. If it has a Job, closing that Job also handles partial setup.
        try:
            close_tree(process)
        except BaseException:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (process.stdout, process.stderr, process.stdin):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        raise
    return process


def _assign_job(process, *, allow_service=False):
    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if allow_service:
        # Only explicit service launchers may request CREATE_BREAKAWAY_FROM_JOB.
        # Ordinary descendants still inherit this Job and are always cleaned.
        limits.BasicLimitInformation.LimitFlags |= 0x00000800
    configured = kernel.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    assigned = configured and kernel.AssignProcessToJobObject(
        job, int(process._handle)
    )
    if not assigned:
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    process._mindie_windows_job = (kernel, job)


def _resume_windows_process(process):
    """Resume only the owned, suspended child after Job assignment.

    NtResumeProcess is also used by psutil's Windows resume implementation.
    The retained Popen handle avoids PID reuse and system-wide thread scans.
    NTSTATUS is converted explicitly; GetLastError is not its error channel.
    """
    import ctypes
    from ctypes import wintypes

    native = ctypes.WinDLL("ntdll")
    resume = native.NtResumeProcess
    resume.argtypes = [wintypes.HANDLE]
    resume.restype = wintypes.LONG
    status = resume(int(process._handle))
    if status < 0:
        convert = native.RtlNtStatusToDosError
        convert.argtypes = [wintypes.LONG]
        convert.restype = wintypes.ULONG
        raise ctypes.WinError(convert(status))


def close_tree(process):
    """Terminate all owned descendants and release the Job handle once."""
    owned = getattr(process, "_mindie_windows_job", None)
    if owned is None:
        # This path is only used for a suspended process whose Job creation
        # failed; it cannot yet have descendants.
        if process.poll() is None:
            process.kill()
        return
    process._mindie_windows_job = None
    kernel, job = owned
    try:
        # The handle has KILL_ON_JOB_CLOSE as a second, unconditional cleanup
        # guarantee, including when the leader exited before this call.
        kernel.TerminateJobObject(job, 1)
    finally:
        if not kernel.CloseHandle(job):
            raise ctypes.WinError(ctypes.get_last_error())
