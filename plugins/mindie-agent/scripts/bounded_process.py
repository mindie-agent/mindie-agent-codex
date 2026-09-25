"""One attempt with an absolute deadline and bounded output; own the child tree.

POSIX reads pipes with a selector and kills the owned process group. Windows
cannot select() process pipes, so it uses two daemon reader threads and kills
the tree with taskkill /T. The Windows path uses only standard primitives but
has not been verified on real hardware yet.
"""

import os
import re
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


def _run_posix(process, timeout, max_output, cancel, allowed_returncodes=(0,), transport=False):
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "out")
    selector.register(process.stderr, selectors.EVENT_READ, "err")
    deadline = time.monotonic() + timeout
    output = bytearray()
    errors = bytearray() if transport else None
    cap = _Cap(max_output)

    def timeout_error():
        err = TimeoutError(
            "MindIE request deadline exceeded; outcome may be unknown; not retried"
        )
        if transport:
            _attach_transport(err, output, errors, timed_out=True)
        return err

    try:
        while selector.get_map():
            if cancel is not None and cancel.is_set():
                raise RuntimeError("MindIE request cancelled; not retried")
            if time.monotonic() >= deadline:
                raise timeout_error()
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
                elif errors is not None:
                    errors.extend(chunk)
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode not in allowed_returncodes:
            err = RuntimeError("MindIE runtime failed; not retried")
            if transport:
                _attach_transport(err, output, errors, timed_out=False)
            raise err
        return output.decode()
    finally:
        _kill_tree(process)
        process.wait(timeout=1)
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _run_windows(process, timeout, max_output, cancel, allowed_returncodes=(0,), transport=False):
    # Windows (unverified on real hardware): reader threads replace selectors.
    deadline = time.monotonic() + timeout
    output = bytearray()
    errors = bytearray() if transport else None
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
        except ValueError as exc:
            failure.append(exc)

    threads = [
        threading.Thread(target=reader, args=(process.stdout, output), daemon=True),
        threading.Thread(target=reader, args=(process.stderr, errors), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("MindIE request cancelled; not retried")
            if time.monotonic() >= deadline:
                err = TimeoutError(
                    "MindIE request deadline exceeded; outcome may be unknown; not retried"
                )
                if transport:
                    _attach_transport(err, output, errors, timed_out=True)
                raise err
            if failure:
                raise failure[0]
            time.sleep(0.02)
        if failure:
            raise failure[0]
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode not in allowed_returncodes:
            err = RuntimeError("MindIE runtime failed; not retried")
            if transport:
                _attach_transport(err, output, errors, timed_out=False)
            raise err
        return bytes(output).decode()
    finally:
        _kill_tree(process)
        process.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()


def run(command, data, *, timeout, max_output=1024 * 1024, cancel=None, env=None, allowed_returncodes=(0,), transport=False):
    if cancel is not None and cancel.is_set():
        raise RuntimeError("MindIE request cancelled before execution")
    with tempfile.TemporaryFile() as stream:
        stream.write(data.encode())
        stream.seek(0)
        process = _spawn(command, stream, env)
        if POSIX:
            return _run_posix(
                process, timeout, max_output, cancel, allowed_returncodes, transport
            )
        return _run_windows(
            process, timeout, max_output, cancel, allowed_returncodes, transport
        )
