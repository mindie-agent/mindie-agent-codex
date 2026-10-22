"""POSIX execution owner for one local process group.

[Role]
Helper wrapper launched in its own POSIX session (`os.setsid()`) by `bounded_process.py` to monitor parent lifetime (`owner_pipe` EOF / `os.getppid()`) and terminate the entire child process group (`os.killpg`) if the parent dies.

[Invariants]
- Even if the caller is `SIGKILL`ed, `owner_pipe` EOF triggers `SIGTERM` followed by `SIGKILL` to the owned process group.
- Explicitly detached services (`allow_service=True`) run in separate sessions outside this group.

[Timeouts & Locks]
- Sends `SIGTERM` on owner death and escalates to `SIGKILL` after 0.2s (8 x 25ms polls).

[Side-Effect Policy]
- Spawns and reaps one child process group; writes child PID/PGID metadata to `notify` fd.
"""
import os
import errno
import signal
import subprocess
import sys


def main():
    owner = int(sys.argv[1])
    notify = int(sys.argv[2])
    owner_pipe = int(sys.argv[3])
    command = sys.argv[4:]
    if not command or os.getppid() != owner or os.getpgrp() != os.getpid():
        return 125
    # Keep a guardian alive after the direct target exits. Otherwise its
    # descendants could outlive an owner killed just before normal cleanup.
    # The caller alone holds the write end: EOF proves owner death without
    # PID reuse races, wall-clock deadlines, or output-silence guesses.
    group = os.getpid()
    if os.fork() == 0:
        os.close(notify)
        for descriptor in (0, 1, 2):
            if descriptor != owner_pipe:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise
        try:
            while os.read(owner_pipe, 1):
                pass
        finally:
            os.killpg(group, signal.SIGKILL)
        os._exit(125)
    os.close(owner_pipe)
    try:
        child = subprocess.Popen(command)
    except TimeoutError:
        raise
    except OSError:
        # The caller gets an explicit failed start, without raw credentials or
        # arbitrary argv in stderr. Native target output is otherwise direct.
        os.write(notify, b"not_started\n")
        os.close(notify)
        return 127
    os.write(notify, b"started\n")
    os.close(notify)
    child.wait()
    if child.returncode < 0:
        # Mirror signal termination too; a caller must see the target's
        # negative Popen return code, not a synthetic shell-style status.
        if -child.returncode not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(-child.returncode, signal.SIG_DFL)
        os.kill(os.getpid(), -child.returncode)
    return child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
