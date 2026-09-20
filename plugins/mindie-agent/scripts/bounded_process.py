"""One attempt with an absolute deadline and bounded output; own the child tree.

POSIX reads pipes with a selector and kills the owned process group. Windows
cannot select() process pipes, so it uses two daemon reader threads and kills
the tree with taskkill /T. The Windows path uses only standard primitives but
has not been verified on real hardware yet.
"""

import os
import subprocess
import tempfile
import threading
import time

POSIX = os.name == "posix"

if POSIX:
    import selectors
    import signal


def _spawn(command, stdin, env):
    if POSIX:
        return subprocess.Popen(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
    # Windows (unverified on real hardware).
    return subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        env=env,
    )


def _kill_tree(process):
    if POSIX:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    # Windows (unverified on real hardware): /T covers owned descendants.
    if process.poll() is None:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            timeout=5,
        )


class _Cap:
    """Shared output-size guard for both reader strategies."""

    def __init__(self, max_output):
        self.max_output = max_output
        self.size = 0

    def add(self, chunk):
        self.size += len(chunk)
        if self.size > self.max_output:
            raise ValueError("MindIE response exceeds output limit; not retried")


def _run_posix(process, timeout, max_output, cancel):
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "out")
    selector.register(process.stderr, selectors.EVENT_READ, "err")
    deadline = time.monotonic() + timeout
    output = bytearray()
    cap = _Cap(max_output)
    try:
        while selector.get_map():
            if cancel is not None and cancel.is_set():
                raise RuntimeError("MindIE request cancelled; not retried")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "MindIE request deadline exceeded; outcome may be unknown; not retried"
                )
            for key, _ in selector.select(
                min(0.05, max(0, deadline - time.monotonic()))
            ):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                cap.add(chunk)
                if key.data == "out":
                    output.extend(chunk)
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            raise RuntimeError("MindIE runtime failed; not retried")
        return output.decode()
    finally:
        _kill_tree(process)
        process.wait(timeout=1)
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _run_windows(process, timeout, max_output, cancel):
    # Windows (unverified on real hardware): reader threads replace selectors.
    deadline = time.monotonic() + timeout
    output = bytearray()
    cap = _Cap(max_output)
    lock = threading.Lock()
    failure = []

    def reader(stream, keep):
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                with lock:
                    cap.add(chunk)
                    if keep:
                        output.extend(chunk)
        except ValueError as exc:
            failure.append(exc)

    threads = [
        threading.Thread(target=reader, args=(process.stdout, True), daemon=True),
        threading.Thread(target=reader, args=(process.stderr, False), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("MindIE request cancelled; not retried")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "MindIE request deadline exceeded; outcome may be unknown; not retried"
                )
            if failure:
                raise failure[0]
            time.sleep(0.02)
        if failure:
            raise failure[0]
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            raise RuntimeError("MindIE runtime failed; not retried")
        return bytes(output).decode()
    finally:
        _kill_tree(process)
        process.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()


def run(command, data, *, timeout, max_output=1024 * 1024, cancel=None, env=None):
    if cancel is not None and cancel.is_set():
        raise RuntimeError("MindIE request cancelled before execution")
    with tempfile.TemporaryFile() as stream:
        stream.write(data.encode())
        stream.seek(0)
        process = _spawn(command, stream, env)
        if POSIX:
            return _run_posix(process, timeout, max_output, cancel)
        return _run_windows(process, timeout, max_output, cancel)
