"""Bound a single optional Codex execution; abort at the first error/tool event."""

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time

MAX_OUTPUT = 128 * 1024
TIMEOUT = 60
ALLOWED_ITEMS = {"agent_message", "reasoning"}


def run_codex(command, prompt):
    if os.name != "posix":
        raise RuntimeError("bounded maintenance requires POSIX process groups")
    # The knowledge service owns one group for worker + Codex + descendants.
    inherited = (
        os.environ.get("MINDIE_MAINTENANCE_GROUP") == "1"
        and os.getpgrp() == os.getpid()
    )
    with tempfile.TemporaryFile() as input_file:
        input_file.write(prompt.encode())
        input_file.seek(0)
        process = subprocess.Popen(
            command,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=not inherited,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "out")
        selector.register(process.stderr, selectors.EVENT_READ, "err")
        deadline = time.monotonic() + TIMEOUT
        pending = bytearray()
        total = turns = 0

        def check_line(line):
            nonlocal turns
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("invalid Codex event")
            if event.get("type") in {"error", "turn.failed"}:
                raise RuntimeError("Codex maintenance failed; no retry")
            if event.get("type") == "turn.started":
                turns += 1
                if turns > 1:
                    raise RuntimeError("maintenance attempted another turn")
            item = event.get("item") or {}
            if item and item.get("type") not in ALLOWED_ITEMS:
                raise RuntimeError("maintenance attempted a tool call")

        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex maintenance deadline exceeded")
                for key, _ in selector.select(
                    min(0.1, max(0, deadline - time.monotonic()))
                ):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT:
                        raise ValueError("Codex maintenance output exceeds limit")
                    if key.data == "out":
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending[:] = rest
                            if line.strip():
                                check_line(line)
            if pending.strip():
                check_line(pending)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code:
                raise RuntimeError(f"Codex maintenance exited {code}; no retry")
        finally:
            if inherited:
                # The service kills this whole group after reading our result/error.
                if process.poll() is None:
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=1)
            selector.close()
            process.stdout.close()
            process.stderr.close()
