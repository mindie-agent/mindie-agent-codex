"""Bounded local spool for component output with a small model-facing preview.

[Role]
Streams child stdout/stderr to private local files and keeps only a bounded preview in memory; produces a receipt (paths, byte counts, sha256) that can be returned even when the run times out, is cancelled or hits the hard cap.

[Invariants]
- Combined spooled bytes never exceed `hard_cap`; the chunk crossing it is stored truncated and `OutputHardCap` is raised so the owner kills the tree.
- A capture writes `<token>-<stream>.active` and renames it to `.spool` on close; an active capture owned by this process is never removed. Only stale `*.active` files older than the orphan threshold are swept.
- A path appears in a receipt only after its file was written and finalized; write/rename failures are reported in `spool_error` and never as a missing artifact claim.
- After `freeze()`/`close()` no more bytes are accepted; late reader-thread chunks are ignored, so bytes, hash and preview stay consistent.
- Filesystem timeouts propagate to the process owner. After execution completes,
  finalization failures are separate cleanup warnings, never a changed exit code.

[Timeouts & Locks]
- One in-process lock serializes `add` against `freeze`. Retention runs when a capture completes (and at start): completed files older than 24h, beyond 64 files or beyond 128 MiB are removed oldest first, never the capture that just completed. Orphan `*.active` sweeps scan at most 256 entries and remove only files older than 24h that this process does not hold open.

[Side-Effect Policy]
- Creates a 0700 directory and 0600 files only under the supplied spool directory; removes only completed `*.spool` files and stale unowned `*.active` orphans there. `_abandon()` also removes its own created-but-unusable temporary file.
"""
import codecs
import hashlib
import os
from pathlib import Path
import threading
import time
import uuid

HARD_CAP = 16 * 1024 * 1024
RETENTION_SECONDS = 24 * 3600
RETENTION_FILES = 64
RETENTION_BYTES = 128 * 1024 * 1024
ACTIVE_ORPHAN_SECONDS = 24 * 3600
ACTIVE_SCAN_LIMIT = 256
ACTIVE_SUFFIX = ".active"
DONE_SUFFIX = ".spool"
_ACTIVE_LOCK = threading.Lock()
_OPEN_ACTIVE = set()


def _remember_active(path):
    with _ACTIVE_LOCK:
        _OPEN_ACTIVE.add(str(path))


def _forget_active(path):
    with _ACTIVE_LOCK:
        _OPEN_ACTIVE.discard(str(path))


def _active_in_use(path):
    with _ACTIVE_LOCK:
        return str(path) in _OPEN_ACTIVE



class OutputHardCap(ValueError):
    """The combined child output exceeded the spool hard cap; not retried."""


def prune(directory, *, now=None, keep=()):
    """Delete completed captures beyond 24h/64 files/128 MiB; return error type names."""
    errors = []
    now = time.time() if now is None else now
    keep = {str(item) for item in keep}
    entries = []
    try:
        names = list(Path(directory).glob("*" + DONE_SUFFIX))
    except TimeoutError:
        raise
    except OSError as exc:
        return [type(exc).__name__]
    for path in names:
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        except TimeoutError:
            raise
        except OSError as exc:
            errors.append(type(exc).__name__)
            continue
        entries.append((info.st_mtime, info.st_size, str(path), path))
    entries.sort(key=lambda item: item[:3])
    total = sum(item[1] for item in entries)
    count = len(entries)
    for mtime, size, name, path in entries:
        if name in keep:
            continue
        if now - mtime < RETENTION_SECONDS and count <= RETENTION_FILES and total <= RETENTION_BYTES:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except TimeoutError:
            raise
        except OSError as exc:
            errors.append(type(exc).__name__)
            continue
        count -= 1
        total -= size
    return errors


def prune_active(directory, *, now=None, keep=()):
    """Remove stale crashed-capture files, never one this process holds open."""
    errors = []
    now = time.time() if now is None else now
    keep = {str(item) for item in keep}
    found = []
    scanned = 0
    try:
        with os.scandir(directory) as iterator:
            for item in iterator:
                if scanned >= ACTIVE_SCAN_LIMIT:
                    break
                scanned += 1
                if item.name.endswith(ACTIVE_SUFFIX):
                    found.append(Path(directory) / item.name)
    except TimeoutError:
        raise
    except OSError as exc:
        return [type(exc).__name__]
    for path in found:
        if str(path) in keep or _active_in_use(path):
            continue
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        except TimeoutError:
            raise
        except OSError as exc:
            errors.append(type(exc).__name__)
            continue
        if now - info.st_mtime < ACTIVE_ORPHAN_SECONDS:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except TimeoutError:
            raise
        except OSError as exc:
            errors.append(type(exc).__name__)
    return errors


class _Stream:
    def __init__(self, name, directory, token, preview_limit):
        self.name = name
        self.path = None
        self.handle = None
        self.size = 0
        self.digest = hashlib.sha256()
        self.preview = bytearray()
        self.preview_limit = preview_limit
        self.truncated = False
        self.render_truncated = False
        self.error = None
        self.closed = False
        try:
            path = Path(directory) / (token + "-" + name + ACTIVE_SUFFIX)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.handle = os.fdopen(descriptor, "wb")
            self.path = str(path)
            _remember_active(path)
        except TimeoutError:
            raise
        except OSError as exc:
            self.error = type(exc).__name__

    def write(self, chunk):
        if self.handle is not None:
            try:
                self.handle.write(chunk)
                self.size += len(chunk)
                self.digest.update(chunk)
            except TimeoutError:
                self.error = "TimeoutError"
                self._abandon()
                raise
            except OSError as exc:
                self.error = type(exc).__name__
                self._abandon()
        room = self.preview_limit - len(self.preview)
        if room > 0:
            self.preview.extend(chunk[:room])
        if len(chunk) > max(room, 0):
            self.truncated = True

    def _abandon(self):
        handle, self.handle = self.handle, None
        path, self.path = self.path, None
        if handle is not None:
            try:
                handle.close()
            except TimeoutError:
                raise
            except OSError:
                pass
        if path is not None:
            _forget_active(path)
            try:
                Path(path).unlink(missing_ok=True)
            except TimeoutError:
                raise
            except OSError:
                pass

    def close(self):
        """Finalize once: close the file and rename `.active` to `.spool`."""
        if self.closed:
            return
        self.closed = True
        handle, self.handle = self.handle, None
        path, self.path = self.path, None
        if handle is None:
            return
        try:
            handle.close()
            final = path[:-len(ACTIVE_SUFFIX)] + DONE_SUFFIX
            os.replace(path, final)
            _forget_active(path)
            self.path = final
        except TimeoutError:
            _forget_active(path)
            self.error = "TimeoutError"
            self.path = None
            raise
        except OSError as exc:
            _forget_active(path)
            self.error = type(exc).__name__
            self.path = None

    def text(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(bytes(self.preview), final=not self.truncated)
        encoded = text.encode("utf-8")
        self.render_truncated = len(encoded) > self.preview_limit
        return encoded[:self.preview_limit].decode("utf-8", errors="ignore")

    def receipt(self):
        value = dict(bytes=self.size, preview_truncated=self.truncated or self.render_truncated)
        if self.path is not None and self.error is None:
            value.update(path=self.path, sha256=self.digest.hexdigest())
        return value


class Capture:
    def __init__(self, directory, preview_budget, *, hard_cap=None):
        self.hard_cap = HARD_CAP if hard_cap is None else hard_cap
        self.total = 0
        self.hard_cap_reached = False
        self.spool_error = None
        self.retention_errors = []
        self.frozen = False
        self.lock = threading.Lock()
        token = "%d-%s" % (int(time.time()), uuid.uuid4().hex)
        try:
            Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
            self.retention_errors = prune(directory)
            self.retention_errors += prune_active(directory)
        except TimeoutError:
            raise
        except OSError as exc:
            self.spool_error = type(exc).__name__
        self.directory = directory
        stdout_limit = max(1, preview_budget * 3 // 4)
        self.streams = dict(
            out=_Stream("stdout", directory, token, stdout_limit),
            err=_Stream("stderr", directory, token, max(1, preview_budget - stdout_limit)),
        )

    def add(self, kind, chunk):
        with self.lock:
            if self.frozen:
                return
            room = self.hard_cap - self.total
            accepted = chunk[:max(room, 0)]
            if accepted:
                self.streams[kind].write(accepted)
                self.total += len(accepted)
            if len(accepted) < len(chunk):
                self.hard_cap_reached = True
                raise OutputHardCap("MindIE component output exceeds the hard cap; not retried")

    def freeze(self):
        """Stop accepting bytes, finalize files and prune; idempotent and thread-safe."""
        with self.lock:
            if self.frozen:
                return
            self.frozen = True
            error = None
            for stream in self.streams.values():
                try:
                    stream.close()
                except TimeoutError as exc:
                    error = error or exc
            if error is not None:
                raise error
            keep = [stream.path for stream in self.streams.values() if stream.path]
            try:
                self.retention_errors += prune(self.directory, keep=keep)
                self.retention_errors += prune_active(self.directory, keep=keep)
            except TimeoutError:
                raise
            except Exception as exc:
                self.retention_errors.append(type(exc).__name__)

    def close(self):
        self.freeze()

    def stdout(self):
        return self.streams["out"].text()

    def stderr(self):
        return self.streams["err"].text()

    def receipt(self):
        value = dict(stdout=self.streams["out"].receipt(), stderr=self.streams["err"].receipt(),
                     hard_cap_bytes=self.hard_cap, hard_cap_reached=self.hard_cap_reached)
        errors = sorted({stream.error for stream in self.streams.values() if stream.error}
                        | ({self.spool_error} if self.spool_error else set()))
        if errors:
            value["spool_error"] = errors
        if self.retention_errors:
            value["retention_error"] = sorted(set(self.retention_errors))
        return value
