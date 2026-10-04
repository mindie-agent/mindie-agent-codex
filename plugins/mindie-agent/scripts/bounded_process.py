"""One owned attempt with bounded output and an optional caller deadline.

POSIX reads pipes with a selector and kills the owned process group. Windows
uses a suspended spawn and owned Job before child code can create descendants.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
import math
import re
import subprocess
import sys
import tempfile
import threading
import time

import windows_process

POSIX = os.name == "posix"

if POSIX:
    import selectors
    import signal


@dataclass
class ProcessResult:
    """One execution outcome; cleanup never changes its execution facts.

    ``completed`` means the target exited and stdout was drained, not that its
    business operation succeeded. Exceptions raised by run carry this same
    object as ``process_result``. A cancelled/disconnected operation is unknown
    unless a higher-level protocol already has its own completion receipt.
    """
    execution: str
    stdout: str = ""
    returncode: int | None = None
    stderr: str = ""
    cleanup: list = field(default_factory=list)

    def checked_stdout(self):
        if self.cleanup:
            raise ProcessCleanupError(self)
        return self.stdout


class ProcessCleanupError(RuntimeError):
    def __init__(self, result):
        self.process_result = result
        super().__init__("Process execution " + result.execution +
                         "; owned-process cleanup failed; do not repeat the operation")


def _cleanup(process, result, *, selector=None, threads=()):
    """Attempt every release and report failures separately from business work."""
    def attempt(stage, operation):
        try:
            operation()
        except Exception as exc:
            result.cleanup.append(dict(stage=stage, error_type=type(exc).__name__))
    attempt("terminate_owned_tree", lambda: _kill_tree(process))
    attempt("reap_process", lambda: process.wait(timeout=1))
    if getattr(process, "_mindie_owner_fd", None) is not None:
        owner_fd, process._mindie_owner_fd = process._mindie_owner_fd, None
        attempt("close_owner_pipe", lambda: os.close(owner_fd))
    if selector is not None:
        attempt("close_selector", selector.close)
    for thread in threads:
        attempt("join_reader", lambda thread=thread: thread.join(timeout=.5))
    for index, stream in enumerate((process.stdout, process.stderr)):
        if threads and threads[index].is_alive():
            result.cleanup.append(dict(stage="join_reader", error_type="ReaderStillRunning"))
        elif stream is not None:
            attempt("close_pipe", stream.close)


def _spawn(command, stdin, env, *, allow_service=False):
    if POSIX:
        from update_lock import generation_descriptors
        started, notify = os.pipe()
        owner_read, owner_write = os.pipe()
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("owned_process.py")),
                 str(os.getpid()), str(notify), str(owner_read), *command],
                stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, env=env,
                pass_fds=(*generation_descriptors(), notify, owner_read),
            )
            process._mindie_owner_fd = owner_write
        except BaseException:
            os.close(started)
            os.close(owner_write)
            raise
        finally:
            os.close(notify)
            os.close(owner_read)
        with os.fdopen(started, 'rb') as stream:
            receipt = stream.read(256)
        if receipt != b"started\n":
            result = ProcessResult("not_started")
            _cleanup(process, result)
            error = OSError("owned command failed before execution")
            error.process_result = result
            raise error
        return process
    # Windows assigns the process to its Job before resuming user code.
    return windows_process.spawn(
        command,
        allow_service=allow_service,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _kill_tree(process):
    if POSIX:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    windows_process.close_tree(process)


class _Cap:
    """Shared output-size guard for both reader strategies."""

    def __init__(self, max_output):
        self.max_output = max_output
        self.size = 0

    def add(self, chunk):
        self.size += len(chunk)
        if self.size > self.max_output:
            raise ValueError("MindIE response exceeds output limit; not retried")


_RETRY_AFTER = re.compile(r"retry-after\s*[:=]\s*(\d{1,6})\b", re.I)
_HTTP_STATUS = re.compile(
    r"(?:http\s*/\s*1\.[01]\s+|status(?:\s+code)?\s*[:=]\s*|"
    r"error\s*[:=]\s*|returned error:\s*|http error\s+|http\s+)(\d{3})\b",
    re.I,
)
_CERT_PHRASES = (
    "certificate verify failed", "sslcertverificationerror",
    "certificate has expired", "self-signed certificate",
    "self signed certificate", "unable to get local issuer certificate",
    "certificate_verify_failed", "ssl: certificate",
    "ssl certificate problem", "unknown ca", "curl: (60)",
)
_RESOLVER_PHRASES = (
    "resolutionimpossible", "conflicting dependencies",
    "package versions have conflicting", "the conflict is caused by",
    "resolver conflict",
)
_HASH_PHRASES = (
    "these packages do not match the hashes", "does not match the hashes",
    "hash mismatch",
)
_GENERIC_CONTENT_PHRASES = (
    "no matching distribution",
    "could not find a version that satisfies",
)
_AUTH_PHRASES = (
    "authentication failed", "could not read username",
    "terminal prompts disabled", "permission denied (publickey)",
    "invalid credentials", "http basic: access denied",
    "support for password authentication was removed",
    "authentication required", "invalid username or password",
)
_RATE_PHRASES = ("rate limit", "too many requests", "secondary rate limit")
_HOOK_PHRASES = ("hook trust", "requires native trust", "changed hooks require")
_CONNECT_PHRASES = (
    "could not resolve host", "temporary failure in name resolution",
    "name or service not known", "nodename nor servname",
    "temporary failure resolving", "network is unreachable",
    "connection timed out", "connection reset by peer", "connection refused",
    "connection aborted", "failed to connect", "operation timed out",
    "read operation timed out", "timed out", "curl: (6)", "curl: (7)",
    "curl: (28)", "curl: (56)", "newconnectionerror",
    "remote end closed connection", "unexpected eof", "connection broken",
    "bad gateway", "service unavailable", "gateway time-out", "gateway timeout",
    "error sending request", "dns error", "recv failure", "send failure",
    "max retries exceeded", "proxy connect aborted",
)


def _has_phrase(text, phrases):
    return any(phrase in text for phrase in phrases)


def classify_transport_text(text):
    """Return (category, retry_after_seconds) or None. Never echoes output."""
    if not isinstance(text, str) or not text.strip():
        return None
    sample = text[:65536]
    lower = sample.lower()
    retry_after = None
    match = _RETRY_AFTER.search(sample)
    if match:
        retry_after = _finite_delay(match.group(1))
    codes = {int(item) for item in _HTTP_STATUS.findall(sample)}
    if _has_phrase(lower, _CERT_PHRASES):
        return ("certificate", None)
    if _has_phrase(lower, _HASH_PHRASES):
        return ("bad_content", None)
    if _has_phrase(lower, _RESOLVER_PHRASES):
        return ("resolver", None)
    if 429 in codes or _has_phrase(lower, _RATE_PHRASES):
        return ("rate_limited", retry_after)
    if 401 in codes or _has_phrase(lower, _AUTH_PHRASES):
        return ("authentication", None)
    if 403 in codes or (
        ("access denied" in lower and "http basic" not in lower)
        or "write access to repository not granted" in lower
        or ("forbidden" in lower and "403" in lower)
        or ("permission denied" in lower and "publickey" not in lower)
    ):
        return ("permission", None)
    if _has_phrase(lower, _HOOK_PHRASES):
        return ("hook_trust", None)
    # A demonstrated transport failure wins over pip's generic final summary.
    if (codes & {500, 502, 503, 504}) or _has_phrase(lower, _CONNECT_PHRASES):
        return ("temporary_network", retry_after)
    if _has_phrase(lower, _GENERIC_CONTENT_PHRASES):
        return ("bad_content", None)
    return None


def _finite_delay(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number == float("inf") or number == float("-inf") or number < 0:
        return None
    return number


_STREAM_EDGE = 4096


def _stream_edges(buf):
    """Head and tail of one stream. The raw bytes are not retained."""
    if not buf:
        return ""
    if isinstance(buf, str):
        text = buf
    else:
        text = bytes(buf).decode("utf-8", "replace")
    if len(text) <= _STREAM_EDGE * 2:
        return text
    return text[:_STREAM_EDGE] + "\n" + text[-_STREAM_EDGE:]


def _joined_output(stdout, stderr):
    parts = []
    for buf in (stdout, stderr):
        piece = _stream_edges(buf)
        if piece:
            parts.append(piece)
    return "\n".join(parts)


def _attach_transport(exc, stdout, stderr, *, timed_out):
    kind = classify_transport_text(_joined_output(stdout, stderr))
    if kind is not None:
        category, retry_after = kind
        exc.category = category
        if retry_after is not None:
            exc.retry_after = retry_after
        return
    if timed_out:
        exc.category = "temporary_network"


def _run_posix(process, timeout, max_output, cancel, allowed_returncodes=(0,), transport=False, on_failure=None):
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "out")
    selector.register(process.stderr, selectors.EVENT_READ, "err")
    deadline = None if timeout is None else time.monotonic() + timeout
    output = bytearray()
    errors = bytearray()
    cap = _Cap(max_output)

    def timeout_error():
        err = TimeoutError(
            "MindIE request deadline exceeded; outcome may be unknown; not retried"
        )
        if transport:
            _attach_transport(err, output, errors, timed_out=True)
        return err

    result = ProcessResult("unknown")
    try:
        while selector.get_map() or process.poll() is None:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("MindIE request cancelled; not retried")
            if deadline is not None and time.monotonic() >= deadline:
                raise timeout_error()
            if process.poll() is not None:
                # The owner exited: inherited pipes are not evidence of a
                # running operation. Close its remaining descendants, then
                # drain the already-produced output to EOF.
                _kill_tree(process)
            for key, _ in selector.select(
                0.05 if deadline is None else min(0.05, max(0, deadline - time.monotonic()))
            ):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                cap.add(chunk)
                if key.data == "out":
                    output.extend(chunk)
                elif errors is not None:
                    errors.extend(chunk)
        # Both EOF and process exit are established. EOF alone must never
        # hide cancellation in an uninterruptible wait.
        process.wait()
        result.execution = "completed"
        result.returncode = process.returncode
        result.stdout = output.decode()
        result.stderr = errors.decode(errors="replace")
        if allowed_returncodes is not None and process.returncode not in allowed_returncodes:
            # A protocol owner may map bounded stdout to a safe error. Raw
            # stderr never leaves this runner, and a nonzero exit still raises.
            err = (on_failure(bytes(output), process.returncode) if on_failure
                   else RuntimeError("MindIE runtime failed; not retried"))
            if not isinstance(err, Exception):
                raise TypeError("process failure mapper must return an exception")
            if transport:
                _attach_transport(err, output, errors, timed_out=False)
            raise err
        return result
    except BaseException as exc:
        result.returncode = process.poll()
        if not result.stdout:
            result.stdout = output.decode(errors="replace")
        exc.process_result = result
        raise
    finally:
        _cleanup(process, result, selector=selector)


def _run_windows(process, timeout, max_output, cancel, allowed_returncodes=(0,), transport=False, on_failure=None):
    # Windows (unverified on real hardware): reader threads replace selectors.
    deadline = None if timeout is None else time.monotonic() + timeout
    output = bytearray()
    errors = bytearray()
    cap = _Cap(max_output)
    lock = threading.Lock()
    failure = []

    def reader(stream, dest):
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                with lock:
                    cap.add(chunk)
                    if dest is not None:
                        dest.extend(chunk)
        except (OSError, ValueError) as exc:
            failure.append(exc)

    threads = [
        threading.Thread(target=reader, args=(process.stdout, output), daemon=True),
        threading.Thread(target=reader, args=(process.stderr, errors), daemon=True),
    ]
    for thread in threads:
        thread.start()
    result = ProcessResult("unknown")
    try:
        while any(thread.is_alive() for thread in threads) or process.poll() is None:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("MindIE request cancelled; not retried")
            if deadline is not None and time.monotonic() >= deadline:
                err = TimeoutError(
                    "MindIE request deadline exceeded; outcome may be unknown; not retried"
                )
                if transport:
                    _attach_transport(err, output, errors, timed_out=True)
                raise err
            if process.poll() is not None:
                _kill_tree(process)
            if failure:
                raise failure[0]
            time.sleep(0.02)
        if failure:
            raise failure[0]
        # Both EOF and process exit are established. EOF alone must never
        # hide cancellation in an uninterruptible wait.
        process.wait()
        result.execution = "completed"
        result.returncode = process.returncode
        result.stdout = bytes(output).decode()
        result.stderr = bytes(errors).decode(errors="replace")
        if allowed_returncodes is not None and process.returncode not in allowed_returncodes:
            # A protocol owner may map bounded stdout to a safe error. Raw
            # stderr never leaves this runner, and a nonzero exit still raises.
            err = (on_failure(bytes(output), process.returncode) if on_failure
                   else RuntimeError("MindIE runtime failed; not retried"))
            if not isinstance(err, Exception):
                raise TypeError("process failure mapper must return an exception")
            if transport:
                _attach_transport(err, output, errors, timed_out=False)
            raise err
        return result
    except BaseException as exc:
        result.returncode = process.poll()
        if not result.stdout:
            result.stdout = bytes(output).decode(errors="replace")
        exc.process_result = result
        raise
    finally:
        _cleanup(process, result, threads=threads)


def run(command, data, *, timeout=None, max_output=1024 * 1024, cancel=None, env=None, allowed_returncodes=(0,), transport=False, allow_service=False, on_failure=None):
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("an explicit execution timeout must be a positive finite number")
    if cancel is not None and cancel.is_set():
        raise RuntimeError("MindIE request cancelled before execution")
    with tempfile.TemporaryFile() as stream:
        stream.write(data.encode())
        stream.seek(0)
        try:
            process = _spawn(command, stream, env, allow_service=allow_service)
        except BaseException as exc:
            if not hasattr(exc, "process_result"):
                exc.process_result = ProcessResult("not_started")
            raise
        if POSIX:
            return _run_posix(
                process, timeout, max_output, cancel, allowed_returncodes, transport, on_failure
            )
        return _run_windows(
            process, timeout, max_output, cancel, allowed_returncodes, transport, on_failure
        )
