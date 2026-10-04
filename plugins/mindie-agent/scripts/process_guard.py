"""Bound a single required Codex execution; abort at the first error/tool event.

One daemon reader thread feeds stdout lines through a queue; a second thread
only counts stderr bytes. This works on POSIX (process groups) and Windows
(suspended spawn assigned to a kill-on-close Job before user code runs).
POSIX additionally supports the service-owned maintenance group contract.
"""

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

import windows_process

MAX_OUTPUT = 128 * 1024
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


def _rejected_model(event):
    """Classify an explicit provider rejection without retaining its message."""
    message = event.get('message')
    error = event.get('error')
    if not isinstance(message, str) and isinstance(error, dict):
        message = error.get('message')
    if not isinstance(message, str):
        return False
    message = message.lower()
    return ('model' in message and ('not supported' in message or 'does not support' in message)
            and ('chatgpt' in message or '400' in message))


def run_codex(command, prompt, *, timeout=None, receipt=None, defer_cleanup=False):
    # The knowledge service owns one group for worker + Codex + descendants.
    started = time.monotonic()
    receipt = {} if receipt is None else receipt
    receipt.update(native_started=False, turn_started=False, turn_completed=False, turn_failed=False,
                   request_rejected=False,
                   usage=None, elapsed_ms=0, cleanup_failed=False)
    inherited = (
        POSIX
        and os.environ.get("MINDIE_MAINTENANCE_GROUP") == "1"
        and os.getpgrp() == os.getpid()
    )
    owner = os.environ.get("MINDIE_MAINTENANCE_OWNER") if inherited else None
    if owner is not None and (not owner.isdecimal() or os.getppid() != int(owner)):
        raise NativeStartError("maintenance owner exited before native execution")
    deferred_to_core = defer_cleanup and (inherited or (
        not POSIX and os.environ.get('MINDIE_MAINTENANCE_GROUP') == '1'
        and os.environ.get('MINDIE_MAINTENANCE_OWNER', '').isdecimal()))
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
                process = windows_process.spawn(
                    command,
                    stdin=input_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        except OSError as exc:
            receipt['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            raise NativeStartError("native executable could not start") from exc
        receipt['native_started'] = True
        lines = queue.Queue()
        total = [0]
        flooded = []
        read_errors = []

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

        def checked_reader(read):
            try:
                read()
            except (OSError, ValueError) as exc:
                read_errors.append(exc)
                lines.put(None)

        threads = [
            threading.Thread(target=checked_reader, args=(read_stdout,), daemon=True),
            threading.Thread(target=checked_reader, args=(read_stderr,), daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        turns = 0
        usage = None

        def check_line(line):
            nonlocal turns, usage
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise InvalidResultError("invalid Codex event") from exc
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise InvalidResultError("invalid Codex event")
            if event.get("type") in {"error", "turn.failed"}:
                receipt['request_rejected'] = _rejected_model(event)
                receipt['turn_failed'] = event.get('type') == 'turn.failed' or receipt['request_rejected']
                raise NativeFailure("Codex maintenance failed; no retry")
            if event.get("type") == "turn.started":
                turns += 1
                receipt['turn_started'] = True
                if turns > 1:
                    raise NativeFailure("maintenance attempted another turn")
            if event.get("type") == "turn.completed":
                if receipt['turn_completed'] and deferred_to_core:
                    code = None  # Core records the worker outcome before tree cleanup.
                elif receipt['turn_completed']:
                    raise NativeFailure("maintenance completed another turn")
                receipt['turn_completed'] = True
                reported = event.get("usage")
                if isinstance(reported, dict):
                    # Allowlisted counters only; never retain native events,
                    # reasoning, credentials or prompt/output text for metering.
                    usage = {key: reported[key] for key in
                             ("input_tokens", "cached_input_tokens", "output_tokens")
                             if type(reported.get(key)) is int and reported[key] >= 0}
                    receipt['usage'] = usage
            item = event.get("item")
            if item is not None and not isinstance(item, dict):
                raise InvalidResultError("invalid Codex item")
            if item and not isinstance(item.get("type"), str):
                raise InvalidResultError("invalid Codex item type")
            if item and item.get("type") not in ALLOWED_ITEMS:
                raise NativeFailure("maintenance attempted a tool call")

        try:
            while True:
                if owner is not None and os.getppid() != int(owner):
                    raise NativeFailure("maintenance owner exited")
                if read_errors:
                    raise InvalidResultError("Codex output read failed") from read_errors[0]
                if flooded or total[0] > MAX_OUTPUT:
                    raise OutputLimitExceeded("Codex maintenance output exceeds limit")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Codex maintenance deadline exceeded")
                try:
                    line = lines.get(timeout=0.1 if remaining is None else min(0.1, remaining))
                except queue.Empty:
                    # Exit is evidence; silence is not. An inherited pipe
                    # holder must not hide the actual native process exit.
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    if flooded:
                        raise OutputLimitExceeded(
                            "Codex maintenance output exceeds limit"
                        )
                    if process.poll() is not None:
                        break
                    continue  # EOF alone does not end healthy native work.
                if line.strip():
                    check_line(line)
                    if receipt['turn_completed']:
                        break
            # A terminal event closes the business operation. Reaping this
            # process afterwards has its own small cleanup wait, never a new
            # model deadline. Preserve terminal evidence on cleanup failure.
            try:
                if receipt['turn_completed']:
                    code = process.wait(timeout=2)
                else:
                    # The event loop already observed actual process exit;
                    # it kept owner checks and explicit deadlines live after EOF.
                    code = process.wait()
            except subprocess.TimeoutExpired as exc:
                receipt['cleanup_failed'] = True
                code = None
            # Validate already-delivered events without waiting for another
            # model turn or treating quiet inference as a fault.
            while True:
                try:
                    pending = lines.get_nowait()
                except queue.Empty:
                    break
                if pending is not None and pending.strip():
                    check_line(pending)
            if code and not receipt['turn_completed']:
                raise NativeFailure("Codex maintenance exited nonzero; no retry")
            if code and receipt['turn_completed']:
                receipt['cleanup_failed'] = True
        except NativeFailure as error:
            # A CLI can emit `error` immediately before `turn.failed`. Inspect
            # only events already queued; never wait for/reconnect the model.
            # Preserve the original failure while recognizing known terminal
            # rejection and avoiding an incorrect unknown-paid-call receipt.
            while True:
                try:
                    pending = lines.get_nowait()
                except queue.Empty:
                    break
                if pending is None:
                    continue
                try:
                    terminal = json.loads(pending)
                except (ValueError, TypeError):
                    continue
                if isinstance(terminal, dict) and terminal.get('type') == 'turn.failed':
                    receipt['turn_failed'] = True
                    receipt['request_rejected'] = receipt['request_rejected'] or _rejected_model(terminal)
            if receipt['request_rejected']:
                error.mindie_category = 'configuration'
            raise
        finally:
            original_error = sys.exc_info()[1]
            if original_error is None and receipt['turn_completed'] and deferred_to_core:
                # The existing core process group owns every descendant. Let
                # this worker deliver the validated business envelope first;
                # the core records it durably before closing the group.
                receipt['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                return usage
            try:
                if inherited:
                    # The service kills this whole group after reading our result/error.
                    if owner is not None and os.getppid() != int(owner):
                        # The owner can no longer reap descendants. This worker
                        # leads only its owned maintenance process group.
                        os.killpg(os.getpid(), signal.SIGKILL)
                    if process.poll() is None:
                        process.kill()
                elif POSIX:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    windows_process.close_tree(process)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        try:
                            process.kill()
                        except OSError:
                            pass
                        process.wait(timeout=2)
                    else:
                        raise
                # A POSIX inherited group's descendants are closed by the outer
                # core. Windows Job cleanup above closes every owned pipe holder.
                if not threads[0].is_alive():
                    process.stdout.close()
                if not threads[1].is_alive():
                    process.stderr.close()
            except Exception:
                receipt['cleanup_failed'] = True
                if original_error is None and not receipt['turn_completed']:
                    raise
            finally:
                receipt['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                if original_error is not None:
                    original_error.mindie_native_receipt = dict(receipt)
        if read_errors:
            if receipt['turn_completed']:
                receipt['cleanup_failed'] = True
            else:
                raise InvalidResultError("Codex output read failed") from read_errors[0]
        return usage
