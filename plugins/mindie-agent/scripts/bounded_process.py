"""One attempt with an absolute deadline and bounded output; own the child group."""

import os
import selectors
import signal
import subprocess
import tempfile
import time


def run(command, data, *, timeout, max_output=1024 * 1024, cancel=None, env=None):
    if os.name != "posix":
        raise RuntimeError("bounded plugin execution requires POSIX process groups")
    if cancel is not None and cancel.is_set():
        raise RuntimeError("MindIE request cancelled before execution")
    with tempfile.TemporaryFile() as stream:
        stream.write(data.encode())
        stream.seek(0)
        process = subprocess.Popen(
            command,
            stdin=stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "out")
        selector.register(process.stderr, selectors.EVENT_READ, "err")
        deadline = time.monotonic() + timeout
        output, size = bytearray(), 0
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
                    size += len(chunk)
                    if size > max_output:
                        raise ValueError(
                            "MindIE response exceeds output limit; not retried"
                        )
                    if key.data == "out":
                        output.extend(chunk)
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode:
                raise RuntimeError("MindIE runtime failed; not retried")
            return output.decode()
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
            selector.close()
            process.stdout.close()
            process.stderr.close()
