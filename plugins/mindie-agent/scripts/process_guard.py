"""Bound a single optional Codex execution; abort at the first error/tool event.

One daemon reader thread feeds stdout lines through a queue; a second thread
only counts stderr bytes. This works on POSIX (process groups) and Windows
(new process group + taskkill tree kill; not yet verified on real hardware).
POSIX additionally supports the service-owned maintenance group contract.
"""

import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time

MAX_OUTPUT = 128 * 1024
TIMEOUT = 120
ALLOWED_ITEMS = {"agent_message", "reasoning"}
POSIX = os.name == "posix"


class OutputLimitExceeded(ValueError):
    """Stdout/stderr grew past the configured byte bound."""


class InvalidResultError(ValueError):
    """A native JSONL event was not a valid object."""


class NativeFailure(RuntimeError):
    """Native child failed, emitted error/turn.failed, or used a forbidden event."""


class NativeStartError(OSError):
    """The native executable could not be started."""


def run_codex(command, prompt):
    # The knowledge service owns one group for worker + Codex + descendants.
    inherited = (
        POSIX
        and os.environ.get("MINDIE_MAINTENANCE_GROUP") == "1"
        and os.getpgrp() == os.getpid()
    )
    with tempfile.TemporaryFile() as input_file:
        input_file.write(prompt.encode())
        input_file.seek(0)
        try:
            if POSIX:
                process = subprocess.Popen(
                    command,
                    stdin=input_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=not inherited,
                )
            else:
                # Windows (unverified on real hardware).
                process = subprocess.Popen(
                    command,
                    stdin=input_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
        except OSError as exc:
            raise NativeStartError("native executable could not start") from exc
        lines = queue.Queue()
        total = [0]
        flooded = []

        def read_stdout():
            # In-memory accumulation stays below MAX_OUTPUT: once the cap is
            # exceeded we stop queueing lines (the queue can never outgrow the
            # cap by more than one line) and signal the consumer, which aborts
            # and kills the process group, so a flooding producer can neither
            # grow memory nor deadlock cancellation.
            while True:
                line = process.stdout.readline(MAX_OUTPUT + 1)
                if not line:
                    break
                total[0] += len(line)
                if total[0] > MAX_OUTPUT:
                    flooded.append(True)
                    lines.put(None)
                    return
                lines.put(line)
            lines.put(None)

        def read_stderr():
            while True:
                chunk = process.stderr.read(8192)
                if not chunk:
                    break
                total[0] += len(chunk)
                if total[0] > MAX_OUTPUT:
                    flooded.append(True)
                    return

        threads = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + TIMEOUT
        turns = 0

        def check_line(line):
            nonlocal turns
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise InvalidResultError("invalid Codex event") from exc
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise InvalidResultError("invalid Codex event")
            if event.get("type") in {"error", "turn.failed"}:
                raise NativeFailure("Codex maintenance failed; no retry")
            if event.get("type") == "turn.started":
                turns += 1
                if turns > 1:
                    raise NativeFailure("maintenance attempted another turn")
            item = event.get("item")
            if item is not None and not isinstance(item, dict):
                raise InvalidResultError("invalid Codex item")
            if item and not isinstance(item.get("type"), str):
                raise InvalidResultError("invalid Codex item type")
            if item and item.get("type") not in ALLOWED_ITEMS:
                raise NativeFailure("maintenance attempted a tool call")

        try:
            while True:
                if flooded or total[0] > MAX_OUTPUT:
                    raise OutputLimitExceeded("Codex maintenance output exceeds limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex maintenance deadline exceeded")
                try:
                    line = lines.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    if process.poll() is not None and not threads[0].is_alive():
                        break
                    continue
                if line is None:
                    if flooded:
                        raise OutputLimitExceeded(
                            "Codex maintenance output exceeds limit"
                        )
                    break
                if line.strip():
                    check_line(line)
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("Codex maintenance deadline exceeded") from exc
            if code:
                raise NativeFailure("Codex maintenance exited nonzero; no retry")
        finally:
            if inherited:
                # The service kills this whole group after reading our result/error.
                if process.poll() is None:
                    process.kill()
            elif POSIX:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                # Windows (unverified on real hardware): /T covers descendants.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=5,
                )
            process.wait(timeout=1)
            # An inherited group's grandchildren can still hold these pipes
            # until the service kills its group. Closing a buffered stream while
            # its reader holds the lock can deadlock the worker's own deadline.
            # Daemon readers end with this short-lived worker; the outer group
            # owner is responsible for all descendants on every exit path.
            if not threads[0].is_alive():
                process.stdout.close()
            if not threads[1].is_alive():
                process.stderr.close()
