#!/usr/bin/env python3
"""Bounded, model-free MindIE updates. Main now; stable GitHub releases later.

Scheduling uses a per-user LaunchAgent on macOS, Task Scheduler on Windows,
and a systemd user timer on Linux. Filesystem publishing uses an atomic
symlink swap on POSIX; on Windows it uses an unprivileged directory junction
with a non-atomic swap covered by the transaction journal. Native Windows
Codex acceptance remains to be recorded separately.
"""

import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import plistlib
import random
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from bounded_process import classify_transport_text, run
import product_contract
from session_gate import config_path, runtime_scripts
from update_lock import file_lock, update_lock

REPOSITORY = "https://github.com/mindie-agent/mindie-agent-codex.git"
LABEL = "org.mindie-agent.plugin-updater"
WIN_TASK = "MindIE Agent Plugin Updater"
SYSTEMD_SERVICE = "mindie-agent-updater.service"
SYSTEMD_TIMER = "mindie-agent-updater.timer"
INTERVAL = 300
ATTEMPTS = 3
_RETRYABLE = frozenset({"temporary_network", "rate_limited"})
_ACTIONABLE = frozenset({"authentication", "permission", "hook_trust", "certificate"})
_QUARANTINE = frozenset({"resolver", "bad_content"})
_KNOWN_FAILURE = _RETRYABLE | _ACTIONABLE | _QUARANTINE
_STATIC_FAILURE = {
    "temporary_network": "temporary network failure",
    "rate_limited": "rate limited",
    "authentication": "authentication failed",
    "permission": "permission denied",
    "hook_trust": "host hook trust required",
    "certificate": "certificate verification failed",
    "resolver": "dependency resolver conflict",
    "bad_content": "package content rejected",
}
# Scheduler cadence is not an execution budget. The process-owned checker
# lock coalesces concurrent ticks while a healthy update or sync continues.


class Incompatible(ValueError):
    pass


def remove_owned_tree(path):
    """Remove a verified updater tree, including Windows read-only Git objects.

    This is only called after the operation establishes tree ownership and
    generation reachability. Sharing violations and other access failures
    remain errors; only a read-only regular file gets one corrected unlink.
    """
    def clear_readonly(function, failed_path, exc_info):
        error = exc_info[1]
        if not isinstance(error, PermissionError) or getattr(error, "winerror", None) != 5:
            raise error
        try:
            mode = os.lstat(failed_path).st_mode
            if not stat.S_ISREG(mode) or mode & stat.S_IWRITE:
                raise error
            os.chmod(failed_path, mode | stat.S_IWRITE)
            function(failed_path)
        except OSError as cleanup_error:
            if cleanup_error is not error:
                error.add_note("Read-only file cleanup also failed: " + type(cleanup_error).__name__)
                raise error from cleanup_error
            raise
    # onerror also supports the documented Python 3.11 runtime baseline.
    shutil.rmtree(path, onerror=clear_readonly)


class InstallRollbackError(RuntimeError):
    """Keep both failed stages; an unproven rollback is never a transport retry."""
    def __init__(self, original, rollback):
        self.original_install_error = dict(stage="install", error=type(original).__name__,
                                           message=str(original)[:240])
        self.rollback_error = dict(stage="rollback", error=type(rollback).__name__,
                                   message=str(rollback)[:240])
        self.rollback_exception = rollback
        super().__init__(f"install failed ({type(original).__name__}); "
                         f"rollback failed ({type(rollback).__name__}); inspect both recorded stages")


_FEED_OK = frozenset({"synced", "unchanged"})
_FEED_PENDING = frozenset({"busy", "deferred"})
_FEED_ERROR = frozenset({"unavailable", "invalid", "exhausted"})
_FEED_PARTIAL = frozenset({"partial"})
_FEED_KNOWN = _FEED_OK | _FEED_PENDING | _FEED_ERROR | _FEED_PARTIAL


def fold_feed_results(output):
    """Fold core `sync` stdout, which MUST be one JSON list (no prefix/suffix).

    Empty list is ok. busy/deferred means not completed now, not a failed
    attempt: all-pending folds to deferred. Mix of completed (synced/unchanged)
    with pending or failed folds to degraded. No completed rows plus at least
    one unavailable/invalid/exhausted folds to sync_failed. A partial commit
    or completed sync with failed cleanup folds to degraded; its original
    completion, failure stage and cleanup receipt remain separate in the row.
    Unknown status or malformed stdout raises.
    """
    text = (output or "").strip()
    if not text:
        raise ValueError("knowledge sync returned empty output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("knowledge sync returned non-JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("knowledge sync result is not a list")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("knowledge sync row is not an object")
        status = item.get("status")
        repo = item.get("repository")
        if not isinstance(status, str) or not status:
            raise ValueError("knowledge sync row missing status")
        if not isinstance(repo, str) or not repo:
            raise ValueError("knowledge sync row missing repository")
        if status not in _FEED_KNOWN:
            raise ValueError("knowledge sync unknown status: " + status)
        rows.append(item)
    good = sum(1 for row in rows if row["status"] in _FEED_OK)
    pending = sum(1 for row in rows if row["status"] in _FEED_PENDING)
    failed = sum(1 for row in rows if row["status"] in _FEED_ERROR or row.get("cleanup_status") == "failed")
    partial = any(row["status"] in _FEED_PARTIAL for row in rows)
    if not rows or (good == len(rows) and not failed):
        aggregate = "ok"
        summary = None
    elif partial or (good and (pending or failed)):
        aggregate = "degraded"
        summary = _feed_error_summary(rows)
    elif failed:
        aggregate = "sync_failed"
        summary = _feed_error_summary(rows)
    elif pending:
        aggregate = "deferred"
        summary = _feed_error_summary(rows)
    else:
        aggregate = "sync_failed"
        summary = _feed_error_summary(rows)
    return aggregate, rows, summary


def _feed_error_summary(rows):
    parts = []
    for row in rows:
        cleanup_failed = row.get("cleanup_status") == "failed"
        if row["status"] in _FEED_OK and not cleanup_failed:
            continue
        details = []
        if row.get("metadata_committed") is True:
            details.append("metadata committed")
        if row.get("failed_stage"):
            details.append("stage=" + str(row["failed_stage"])[:80])
        if cleanup_failed:
            details.append("cleanup failed: " + str(row.get("cleanup_error") or "unknown cause")[:80])
        detail = row.get("detail") or row.get("cause")
        if detail:
            details.append(str(detail)[:80])
        parts.append(f"{row['repository']}:{row['status']}:" + ", ".join(details))
    return "; ".join(parts)[:240]


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_text(path, value):
    """Publish a complete updater-owned text file by same-directory replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _remove_link(path):
    """Remove an updater-owned link (file/dir symlink or junction), never real data."""
    if not path.is_symlink():
        if os.name == "nt" and path.is_dir():
            attributes = os.stat(path, follow_symlinks=False).st_file_attributes
            if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT: junction
                os.rmdir(path)
                return
            raise Incompatible(f"refusing to replace real directory: {path}")
        if not path.exists():
            return
    path.unlink()


def link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".next")
    _remove_link(temporary)
    if os.name == "nt":
        # Windows (unverified on real hardware): plain users lack
        # SeCreateSymbolicLinkPrivilege, but a directory junction needs no
        # privilege. Junctions cannot be atomically replaced, so the stale
        # link is removed first; the transaction journal re-creates it if the
        # process dies in between.
        run(["cmd", "/d", "/c", "mklink", "/J", str(temporary), str(target)],
            "").checked_stdout()
        _remove_link(path)
        os.replace(temporary, path)
        return
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, path)


def _retry_delay(count, retry_after=None):
    """Scheduler-scale exponential delay plus jitter. One later check, no loop."""
    failures = max(1, int(count))
    delay = min(INTERVAL * (2 ** min(failures - 1, 6)), 6 * 3600)
    delay += random.uniform(0, max(1.0, delay * 0.1))
    if retry_after is not None:
        try:
            delay = max(delay, float(retry_after))
        except (TypeError, ValueError):
            pass
    return delay


def _failure_category(exc):
    category = getattr(exc, "category", None)
    if category in _KNOWN_FAILURE:
        return category
    return None


def _classified_error(category, retry_after=None):
    err = RuntimeError(_STATIC_FAILURE.get(category, "update source rejected"))
    err.category = category
    if retry_after is not None:
        err.retry_after = retry_after
    return err


def _finite_seconds(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number == float("inf") or number == float("-inf") or number < 0:
        return None
    return number


def _bounded_header(headers, name):
    """One short header value. Nothing raw is retained by the caller."""
    if headers is None:
        return None
    try:
        raw = headers.get(name)
    except Exception:
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > 128 or any(char in text for char in "\r\n\x00"):
        return None
    return text


def _header_retry_after(headers):
    """Retry-After as delta-seconds or HTTP-date. Large finite delays are kept."""
    text = _bounded_header(headers, "Retry-After")
    if text is None:
        return None
    if text[:1].isdigit():
        return _finite_seconds(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = (when - datetime.now(timezone.utc)).total_seconds()
    if delay < 0:
        delay = 0
    return _finite_seconds(delay)


def _rate_limit_delay(headers):
    """Explicit rate-limit header evidence and a safe delay, or (False, None)."""
    retry_after = _header_retry_after(headers)
    remaining = _bounded_header(headers, "X-RateLimit-Remaining")
    reset = _bounded_header(headers, "X-RateLimit-Reset")
    remaining_zero = False
    if remaining is not None:
        try:
            remaining_zero = int(remaining) == 0
        except ValueError:
            remaining_zero = False
    reset_delay = None
    if remaining_zero and reset is not None:
        stamp = _finite_seconds(reset)
        if stamp is not None:
            if stamp > 1_000_000_000:
                delay = stamp - datetime.now(timezone.utc).timestamp()
                reset_delay = _finite_seconds(0 if delay < 0 else delay)
            else:
                reset_delay = stamp
    if retry_after is None:
        retry_after = reset_delay
    return bool(retry_after is not None or remaining_zero), retry_after


def _http_failure(code, headers, body):
    """Static category for one release response. Body and headers are not kept."""
    limited, retry_after = _rate_limit_delay(headers)
    if code == 403 and limited:
        return _classified_error("rate_limited", retry_after)
    text = body if isinstance(body, str) else ""
    classified = _classify_release_failure(f"HTTP {code}\n{text[:8192]}")
    if classified is not None:
        if (
            getattr(classified, "retry_after", None) is None
            and retry_after is not None
            and classified.category in {"rate_limited", "temporary_network"}
        ):
            classified.retry_after = retry_after
        return classified
    if code in {500, 502, 503, 504}:
        return _classified_error("temporary_network", retry_after)
    if code == 429:
        return _classified_error("rate_limited", retry_after)
    if code == 401:
        return _classified_error("authentication")
    if code == 403:
        return _classified_error("permission")
    return None


def _classify_release_failure(text):
    kind = classify_transport_text(text)
    if kind is None:
        return None
    return _classified_error(kind[0], kind[1])


def venv_python(venv):
    """The interpreter uv creates inside a venv, on either platform."""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def stop_hook_commands(argv):
    """Keep Stop neutral while exposing failures before diagnostics can start.

    Codex accepts a Windows-specific command override. The POSIX command and
    its Windows counterpart discard protocol output, but report a fixed,
    bounded error when the helper fails. Missing executables or scripts cannot
    leave an incident in the Python diagnostic channel.
    """
    argv = [str(value) for value in argv]
    failure = "MindIE Stop helper failed; capture completion is unconfirmed; no automatic retry."
    def posix_arg(value):
        if value.startswith("${PLUGIN_ROOT}/"):
            return '"${PLUGIN_ROOT}"' + shlex.quote(value[len('${PLUGIN_ROOT}'):])
        return shlex.quote(value)
    posix = ("if ! " + " ".join(posix_arg(arg) for arg in argv) + " >/dev/null 2>&1; then printf '%s\\n' "
             + shlex.quote(failure) + " >&2; fi; printf '{}\\n'")
    # Native Codex can dispatch Windows hooks through PowerShell. CMD's
    # `& echo` becomes a background job there and loses the event on stdin.
    # An encoded PowerShell command has one unambiguous argv under either
    # host shell; the child inherits stdin and every exit remains neutral.
    def ps_arg(value):
        if value.startswith("${PLUGIN_ROOT}/"):
            return "(Join-Path $env:PLUGIN_ROOT '" + value[len('${PLUGIN_ROOT}/'):].replace("'", "''") + "')"
        return "'" + value.replace("'", "''") + "'"
    warning = "[Console]::Error.WriteLine(" + ps_arg(failure) + ")"
    # Join-Path may auto-load its module and serialize first-use progress to
    # stderr. Progress is not a helper failure; keep the actual error channel.
    body = ("$ProgressPreference = 'SilentlyContinue'; try { & " + " ".join(ps_arg(arg) for arg in argv)
            + " 1>$null 2>$null; if ($LASTEXITCODE -ne 0) { " + warning
            + " } } catch { " + warning
            + " } finally { [Console]::Out.WriteLine('{}') }; exit 0")

    windows = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(body.encode("utf-16le")).decode("ascii")
    return {"command": posix, "commandWindows": windows, "statusMessage": "MindIE Agent"}



class Updater:
    def __init__(self, settings):
        self.settings_path = Path(settings).absolute()
        self.settings = read(settings)
        self.root = Path(self.settings["root"])
        self.config = Path(self.settings["adapter_config"])
        self.state_path = self.root / "state.json"
        self.state = read(self.state_path, {})

    def command(self, args, *, timeout=None, data="", allowed_returncodes=(0,), transport=False, allow_service=False, max_output=1024 * 1024, on_failure=None):
        # Disable implicit download/transport retries. Each scheduler run gets one attempt.
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "PYTHONPATH"
        }
        env.update(
            GIT_TERMINAL_PROMPT="0",
            UV_HTTP_RETRIES="0",
            UV_NO_PROGRESS="1",
            MINDIE_AGENT_CONFIG=str(self.config),
            CODEX_HOME=self.settings["codex_home"],
            MINDIE_CODEX_BIN=self.settings["codex"],
        )
        completed = run(
            [str(arg) for arg in args], data, timeout=timeout, env=env,
            allowed_returncodes=allowed_returncodes, transport=transport,
            allow_service=allow_service,
            max_output=max_output, on_failure=on_failure,
        )
        if completed.cleanup:
            self.state["process_cleanup"] = dict(
                status="failed", execution=completed.execution, returncode=completed.returncode,
                issues=completed.cleanup, automatic_retry=False)
        return completed.stdout

    def save(self, status, **values):
        self.state.update(status=status, checked_at=time.time(), **values)
        if status in {"installed", "up_to_date"}:
            if (self.state.get("service_handoff") or {}).get("status") in {"failed", "pending"}:
                self.state["status"] = "degraded"
            if self.state.get("launcher_error") or self.state.get("process_cleanup") or self.state.get("state_persistence"):
                self.state["status"] = "partial"
        if getattr(self, "_state_write_error", None) is not None:
            raise self._state_write_error
        try:
            atomic(self.state_path, self.state)
        except Exception as exc:
            self._state_write_error = exc
            self.state["state_persistence"] = dict(status="failed", error_type=type(exc).__name__,
                generation_committed=bool(getattr(self, "_generation_committed", False)), automatic_retry=False)
            raise
        return self.state

    def _read_release(self, request):
        """Read release JSON. Failure text is classified and then discarded."""
        try:
            with urllib.request.urlopen(request) as response:
                return response.read(256 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(8192)
                text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else ""
            except Exception:
                text = ""
            classified = _http_failure(exc.code, exc.headers, text)
            if classified is not None:
                raise classified from None
            raise ValueError("release metadata rejected") from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise _classified_error("temporary_network") from None
            classified = _classify_release_failure(str(reason)[:4000])
            if classified is not None:
                raise classified from None
            raise ValueError("release source unavailable") from None
        except (TimeoutError, socket.timeout):
            raise _classified_error("temporary_network") from None

    def resolve(self):
        ref = "refs/heads/main"
        if self.settings["channel"] == "release":
            # GitHub's latest endpoint excludes drafts and prereleases.
            request = urllib.request.Request(
                "https://api.github.com/repos/mindie-agent/mindie-agent-codex/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": LABEL},
            )
            raw = self._read_release(request)
            if len(raw) > 256 * 1024:
                raise ValueError("release metadata too large")
            tag = json.loads(raw)["tag_name"]
            if not isinstance(tag, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
                raise ValueError("release tag must declare a normal product version")
            self._release_version = tag.removeprefix("v")
            ref = "refs/tags/" + tag
        elif self.settings["channel"] != "main":
            raise ValueError("unsupported update channel")
        output = self.command(
            ["git", "ls-remote", self.settings["repository"], ref, ref + "^{}"],
            transport=True,
        )
        refs = dict(line.split()[::-1] for line in output.splitlines())
        sha = refs.get(ref + "^{}", refs.get(ref, ""))
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("update source did not resolve to a commit")
        return sha

    def validate_source(self, source):
        try:
            product_contract.identity(source)
            if self.settings["channel"] == "release":
                version = read(Path(source) / "plugins/mindie-agent/.codex-plugin/plugin.json")["version"]
                if (not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version)
                        or version != getattr(self, "_release_version", version)):
                    raise ValueError("release source version must match its normal release tag")
        except (OSError, ValueError) as exc:
            raise Incompatible("invalid product combination: " + str(exc)) from exc

    def probe_runtime(self, python, scripts=None, *, revision=None, verified_receipt=None):
        """Execute the candidate's validator, never this updater's private probe."""
        return product_contract.probe(python, scripts or Path(__file__).parent,
                                      self.command, revision=revision, verified_receipt=verified_receipt)

    def prepare(self, sha):
        generation = self.root / "generations" / sha
        receipt = generation / "prepared.json"
        if receipt.exists():
            candidate = read(receipt)
            self.validate_source(generation / "source")
            expected = product_contract.identity(generation / "source", sha)
            product_contract.validate_receipt(json.dumps(candidate.get("validation")), expected)
            if candidate.get("revision") != sha or candidate.get("source") != str(generation / "source"):
                raise Incompatible("prepared generation identity differs from its source")
            return candidate
        if generation.exists():
            marker = read(generation / "ownership.json", {})
            if marker != {"schema": "mindie-runtime-generation/2", "revision": sha}:
                raise Incompatible("incomplete generation has no verified ownership record")
            remove_owned_tree(generation)  # Only our uncommitted, incomplete staging area.
        source = generation / "source"
        source.mkdir(parents=True)
        atomic(generation / "ownership.json", {"schema": "mindie-runtime-generation/2", "revision": sha})
        self.command(["git", "init", "-q", source])
        self.command(
            [
                "git",
                "-C",
                source,
                "fetch",
                "--no-auto-maintenance",
                "--depth=1",
                self.settings["repository"],
                sha,
            ],
            transport=True,
        )
        self.command(["git", "-C", source, "checkout", "--detach", "-q", "FETCH_HEAD"])
        if self.command(["git", "-C", source, "rev-parse", "HEAD"]).strip() != sha:
            raise ValueError("fetched revision changed")
        self.validate_source(source)
        python = venv_python(generation / "venv")
        uv = self.settings["uv"]
        self.command(
            [uv, "venv", "--python", self.settings["python"], generation / "venv"],
        )
        self.command(
            [
                uv,
                "pip",
                "install",
                "--python",
                python,
                "-r",
                source / "runtime-requirements.txt",
            ],
            transport=True,
        )
        validation = self.probe_runtime(python, source / "plugins/mindie-agent/scripts", revision=sha)
        return self.package(generation, source, python, sha, validation)

    def package(self, generation, source, python, revision, validation):
        expected = product_contract.identity(source, validation.get("candidate_revision"))
        product_contract.validate_receipt(json.dumps(validation), expected)
        plugin = generation / "plugin"
        shutil.copytree(
            source / "plugins/mindie-agent",
            plugin,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for filename in ("product-contract.json", "runtime-requirements.txt"):
            shutil.copy2(source / filename, plugin / filename)
        manifest_path = plugin / ".codex-plugin/plugin.json"
        manifest = read(manifest_path)
        if manifest["name"] != "mindie-agent":
            raise Incompatible("unexpected plugin name")
        # Native discovery selects the numerically greatest build metadata
        # among cached version directories. The fixed-width microsecond UTC
        # timestamp preserves that numeric width across normal updates. Clock
        # skew or manually prepared versions can still sort differently, so
        # install verifies the actual native selection instead of guessing.
        manifest["version"] = (
            manifest["version"].split("+")[0]
            + "+codex."
            + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        )
        atomic(manifest_path, manifest)
        # Native tasks retain this installation-level path across updates.
        atomic(generation / "ownership.json", {"schema": "mindie-runtime-generation/2", "revision": generation.name})
        config_value = str(Path(self.config).expanduser().absolute())
        atomic(
            plugin / "scripts" / "installation.json",
            {"adapter_config": config_value},
        )
        atomic(
            plugin / "scripts" / "diagnostic-build.json",
            {"revision": revision, "version": manifest["version"]},
        )
        mcp = read(plugin / ".mcp.json")
        for name, server in mcp["mcpServers"].items():
            server["command"] = self.settings["python"]
            role = {"mindie-knowledge": "knowledge-mcp", "mindie-remote-dev": "remote-mcp"}[name]
            server["args"] = [str(self.root / "runtime_launcher.py"), "--config", config_value, role]
            server["cwd"] = str(self.root)
            env = dict(server.get("env") or {})
            env["MINDIE_AGENT_CONFIG"] = config_value
            server["env"] = env
            server.pop("env_vars", None)
        atomic(plugin / ".mcp.json", mcp)
        argv = [
            self.settings["python"],
            str(self.root / "runtime_launcher.py"),
            "--config",
            config_value,
            "stop",
        ]
        atomic(
            plugin / "hooks/hooks.json",
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", **stop_hook_commands(argv)}
                            ]
                        }
                    ]
                }
            },
        )
        result = dict(
            revision=revision,
            source=str(source),
            validation=validation,
            package_sha256=product_contract.source_identity(plugin),
            plugin=str(plugin),
            python=str(python),
            version=manifest["version"],
        )
        atomic(generation / "prepared.json", result)
        return result

    def marketplace(self):
        result = json.loads(
            self.command(
                [self.settings["codex"], "plugin", "marketplace", "list", "--json"]
            )
        )
        return next(
            (m for m in result["marketplaces"] if m["name"] == "mindie-agent"), None
        )

    def register(self, source):
        existing = self.marketplace()
        if existing and Path(existing["root"]).resolve() != Path(source).resolve():
            if existing.get("marketplaceSource", {}).get("sourceType") != "local":
                raise Incompatible("refusing to replace a non-local marketplace")
            self.command(
                [
                    self.settings["codex"],
                    "plugin",
                    "marketplace",
                    "remove",
                    "mindie-agent",
                    "--json",
                ]
            )
        self.command(
            [self.settings["codex"], "plugin", "marketplace", "add", source, "--json"]
        )

    def validate_marketplace(self, existing):
        if not existing:
            return
        source_type = (existing.get("marketplaceSource") or {}).get("sourceType")
        if source_type == "local":
            return
        # Native Windows 0.158 lists only name/root. An omitted optional
        # field is sufficient for neither rejection nor migration authority:
        # accept only this updater's exact managed marketplace directory.
        if source_type is None and isinstance(existing.get("root"), str):
            if Path(existing["root"]).resolve() == (self.root / "marketplace").resolve():
                return
        raise Incompatible("only the existing local MindIE marketplace can be migrated")

    def native_plugin_entry(self):
        """Actual native inventory entry for mindie-agent@mindie-agent, or None.

        The install JSON receipt is not proof of final state: native discovery
        scans the version directories under plugins/cache at LIST time and
        selects the highest (base version, numeric build metadata) directory
        name, independent of manifest content (verified against codex-cli
        0.153.4 in an isolated CODEX_HOME). Only a fresh `plugin list` shows
        what the host actually resolved.
        """
        try:
            result = json.loads(
                self.command(
                    [
                        self.settings["codex"],
                        "plugin",
                        "list",
                        "--json",
                        "--marketplace",
                        "mindie-agent",
                    ],
                        )
            )
        except Exception as exc:
            raise RuntimeError("native plugin inventory could not be read") from exc
        if not isinstance(result, dict) or not isinstance(result.get("installed"), list):
            raise RuntimeError("native plugin inventory has an invalid shape")
        for entry in result.get("installed", []):
            if entry.get("pluginId") == "mindie-agent@mindie-agent":
                return entry
        return None

    def _require_selection(self, entry, version):
        if (
            not entry
            or entry.get("name") != "mindie-agent"
            or entry.get("enabled") is not True
            or entry.get("installed") is not True
            or entry.get("version") != version
        ):
            raise RuntimeError(
                "native plugin selection is "
                + json.dumps(
                    None
                    if not entry
                    else {
                        key: entry.get(key)
                        for key in ("version", "enabled", "installed")
                    }
                )
                + ", expected installed+enabled version "
                + version
            )
        return entry

    def verify_native(self, version, plugin=None):
        """Acceptance of a candidate: fail unless native inventory resolves
        exactly this installed version AND the resolved cache tree carries
        the candidate's bytes.

        The version string alone is not proof: a stale cache directory with
        the same version name could hold older bytes. The candidate plugin
        tree comes from the argument or the recorded current generation; when
        no tree can be named, acceptance fails — a bare version match is
        never a warning-level pass.
        """
        entry = self._require_selection(self.native_plugin_entry(), version)
        if plugin is None:
            plugin = self.state.get("current", {}).get("plugin")
        if not isinstance(plugin, str) or not plugin:
            raise RuntimeError(
                "native byte verification requires the candidate plugin tree; "
                "a bare version match is not acceptance"
            )
        resolved = (
            Path(self.settings["codex_home"])
            / "plugins/cache/mindie-agent/mindie-agent"
            / version
        )
        self._verify_tree_bytes(Path(plugin), resolved)
        return entry

    def confirm_native(self, version):
        """Recovery confirmation that a previously resolved native state was
        restored: version/enabled/installed selection only. Byte verification
        of a version installed by this updater happened at its own install;
        pre-contract retained caches have no local generation tree to verify
        against."""
        return self._require_selection(self.native_plugin_entry(), version)

    @staticmethod
    def _verify_tree_bytes(candidate, resolved):
        """Every candidate file must exist in the resolved cache, byte-equal.

        Extra files the host adds to its cache are tolerated; a missing or
        differing candidate file means the host would execute bytes other
        than the reviewed generation.
        """
        candidate = Path(candidate)
        if not candidate.is_dir():
            raise RuntimeError("candidate plugin tree is missing: " + str(candidate))
        mismatches = []
        for path in sorted(candidate.rglob("*")):
            # Python imports may create bytecode after the source was
            # packaged. It is excluded from packaging and is not a shipped
            # file; source and installed dependency bytes remain verified.
            if "__pycache__" in path.relative_to(candidate).parts or not path.is_file():
                continue
            relative = path.relative_to(candidate)
            other = resolved / relative
            try:
                same = other.is_file() and other.read_bytes() == path.read_bytes()
            except OSError:
                same = False
            if not same:
                mismatches.append(str(relative))
            if len(mismatches) >= 5:
                break
        if mismatches:
            raise RuntimeError(
                "resolved native cache does not carry the candidate bytes at "
                + str(resolved)
                + "; differing: "
                + ", ".join(mismatches)
            )

    def project_publication(self, declaration):
        """Journal one contract field under the shared settings write lock."""
        import sharing
        with sharing.community_write_lock(self.config):
            path = sharing.configured_path(self.config)
            if not path.exists():
                return
            value = read(path)
            if not isinstance(value, dict):
                raise ValueError("community settings must be an object")
            publication = declaration["publication"]
            if value.get("repository") != publication["repository"]:
                return
            key = "publication_contract_sha256"
            desired = publication["contract_sha256"]
            if value.get(key) == desired:
                return
            journal_path = self.root / "transaction.json"
            journal = read(journal_path)
            journal["publication_projection"] = dict(
                path=str(path), repository=publication["repository"],
                present=key in value, previous=value.get(key), written=desired)
            atomic(journal_path, journal)
            value[key] = desired
            atomic(path, value)

    def restore_publication(self, journal):
        import sharing
        change = journal.get("publication_projection")
        if not change:
            return
        with sharing.community_write_lock(self.config):
            path = Path(change["path"])
            value = read(path)
            key = "publication_contract_sha256"
            if not isinstance(value, dict) or value.get("repository") != change["repository"]:
                raise RuntimeError("community authority changed during product rollback")
            previous = change["previous"] if change["present"] else None
            if value.get(key) == previous and (key in value) == change["present"]:
                return  # Journal persisted before the field write, or already restored.
            if value.get(key) != change["written"]:
                raise RuntimeError("publication contract changed during product rollback")
            if change["present"]:
                value[key] = previous
            else:
                value.pop(key, None)
            atomic(path, value)

    def recover(self):
        journal_path = self.root / "transaction.json"
        if not journal_path.exists():
            return
        journal = read(journal_path)
        prepared = journal.get("candidate_generation")
        selected = read(self.config)
        if (isinstance(prepared, dict) and journal.get("generation_committed") is True
                and selected.get("runtime_scripts") == str(Path(prepared["plugin"]) / "scripts")
                and selected.get("product_validation") == prepared.get("validation")):
            # Native install and the adapter pointer may be proven complete
            # while the final state save failed. Reconcile that exact result,
            # never reinstall or infer a rollback from the missing bookkeeping.
            self.verify_native(prepared["version"], prepared["plugin"])
            self._generation_committed = True
            self.state.pop("state_persistence", None)
            self.save("installed", current=prepared, candidate=prepared["revision"])
            handoff = self.state.get("service_handoff")
            if isinstance(handoff, dict) and isinstance(handoff.get("retirement"), dict):
                self.restore_service(True, handoff, prepared)
            journal_path.unlink()
            return
        if self.state.get("current", {}).get("revision") == journal.get("candidate"):
            self.verify_native(
                self.state["current"]["version"],
                self.state["current"].get("plugin"),
            )
            journal_path.unlink()
            return
        if journal.get("recoveries", 0) >= ATTEMPTS:
            raise RuntimeError(
                "rollback needs operator attention; automatic recovery exhausted"
            )
        journal["recoveries"] = journal.get("recoveries", 0) + 1
        atomic(journal_path, journal)
        self.restore_publication(journal)
        atomic(self.config, journal["adapter"])
        if journal.get("link"):
            link(self.root / "marketplace/plugins/mindie-agent", journal["link"])
        if journal.get("marketplace"):
            self.register(journal["marketplace"])
            self.command(
                [
                    self.settings["codex"],
                    "plugin",
                    "add",
                    "mindie-agent@mindie-agent",
                    "--json",
                ]
            )
        # Retained caches must not pollute the rollback either: the native
        # selection after recovery has to be the version the journal restored.
        if journal.get("marketplace") and journal.get("previous_version"):
            self.confirm_native(journal["previous_version"])
        journal_path.unlink()

    def restore_service(self, final_proven, handoff, candidate):
        """No lock-recursive launcher; one restore with the actual adapter tuple."""
        try:
            if not final_proven:
                raise RuntimeError("native recovery is unproven")
            selected = read(self.config)
            if selected["engine_config"] == handoff["engine_config"]:
                # The candidate owns the new retirement protocol, but the
                # selected generation must own the restored service process.
                # This also supports rollback to a pre-retirement core without
                # running its service under the candidate interpreter.
                restored = json.loads(self.command(
                    [candidate["python"], Path(candidate["plugin"]) / "scripts/service_handoff.py",
                     "unretire", selected["engine_config"], json.dumps(handoff["retirement"])]))
                if restored.get("status") not in {"restored", "not-needed"}:
                    raise RuntimeError("exact retired configuration could not be restored")
            output = self.command(
                [selected["python"], Path(selected["runtime_scripts"]) / "service_handoff.py",
                 "restore", selected["engine_config"]], allow_service=True)
            result = json.loads(output)
            if result.get("status") not in {"restored", "not-needed"}:
                raise RuntimeError("invalid restoration result")
        except Exception as exc:
            result = dict(status="failed", error=type(exc).__name__)
        self.save(self.state.get("status", "update_failed"), service_handoff=dict(handoff, restoration=result, status=result["status"]))

    def prepare_capture(self, candidate):
        helper = Path(candidate["plugin"]) / "scripts/capture_config.py"
        output = self.command([candidate["python"], helper])
        result = json.loads(output)
        if not isinstance(result, dict) or result.get("capture_mode") != "public-transcript":
            raise ValueError("candidate capture preparation returned an invalid receipt")
        return result

    def validate_state_compatibility(self, candidate, adapter):
        helper = Path(candidate["plugin"]) / "scripts/state_compatibility.py"
        if not helper.is_file():
            raise Incompatible("candidate lacks its persisted-state compatibility check")
        result = json.loads(self.command([candidate["python"], helper, adapter["engine_config"]]))
        if result != {"status": "compatible"}:
            raise Incompatible("candidate did not confirm persisted-state compatibility")

    def install(self, candidate):
        # Actual-idle switching: the exclusive operation lock waits for any
        # in-flight admitted call (holders of the shared lock), and the idle
        # probe waits for maintenance and in-flight publication. Idle task
        # authorizations — enabled leases with no running work — never
        # block an update; unreadable admission state fails their calls
        # closed but is not an update concern either.
        with update_lock(self.config, exclusive=True):
            adapter = read(self.config)
            existing = self.marketplace()
            # Validate before stopping a working service or writing a journal.
            self.validate_marketplace(existing)
            self.validate_source(candidate["source"])
            expected = product_contract.identity(candidate["source"], candidate["validation"].get("candidate_revision"))
            product_contract.validate_receipt(json.dumps(candidate["validation"]), expected)
            if product_contract.source_identity(candidate["plugin"]) != candidate.get("package_sha256"):
                raise Incompatible("prepared plugin bytes changed after validation")
            self.probe_runtime(candidate["python"],
                               Path(candidate["source"]) / "plugins/mindie-agent/scripts",
                               revision=candidate["validation"].get("candidate_revision"),
                               verified_receipt=candidate["validation"])
            self.validate_state_compatibility(candidate, adapter)
            # Dependency preparation happens only for an owned installation
            # and before stopping its service. Stop hooks never download.
            capture_config = self.prepare_capture(candidate)
            idle_helper = Path(candidate["plugin"]) / "scripts/service_handoff.py"
            if not idle_helper.is_file():
                raise Incompatible("updater is missing service_handoff.py")
            self.publish_runtime_launcher(candidate)
            handoff = dict(status="pending", engine_config=adapter["engine_config"],
                           error="interrupted-retirement-needs-inspection", automatic_retry=False)
            self.save(self.state.get("status", "preparing"), service_handoff=handoff)
            try:
                idle = json.loads(self.command(
                    [candidate["python"], idle_helper, "stop", adapter["engine_config"]],
                    allowed_returncodes=(0, 1)))
            except Exception as exc:
                self.save("update_failed", service_handoff=dict(handoff,
                    status="failed", error="retirement-outcome-unconfirmed", error_type=type(exc).__name__))
                raise RuntimeError("retirement outcome unconfirmed; inspect before recovery") from exc
            receipt = idle.get("retirement")
            handoff = dict(handoff, observation=idle, retirement=receipt)
            if (idle.get("status") == "failed" or not isinstance(receipt, dict)
                    or receipt.get("status") not in {"retired", "busy"}):
                self.save("update_failed", service_handoff=dict(handoff, status="failed"))
                raise RuntimeError("service retirement is unconfirmed; no automatic retry")
            if not idle.get("idle"):
                return self.save("waiting_for_idle", candidate=candidate["revision"],
                                 service_handoff=dict(handoff, status="busy"))
            # Persist the known effect before publication. An absent old
            # listener is still retired, and therefore needs restoration.
            handoff["status"] = "retired"
            try:
                self.save(self.state.get("status", "preparing"), service_handoff=handoff)
            except Exception as primary:
                if not idle.get("cleanup_failed"):
                    try:
                        self.restore_service(True, handoff, candidate)
                    except Exception as restore_error:
                        primary.add_note("Retired service restoration bookkeeping failed: " + type(restore_error).__name__)
                raise
            if idle.get("cleanup_failed"):
                self.save("update_failed", service_handoff=dict(handoff, status="failed",
                    error="retirement-cleanup-failed"))
                raise RuntimeError("service retired but cleanup failed; no publication attempted")
            installed = False
            final_proven = True  # prior native state, before any install mutation
            try:
                from capture_config import store_failure
                # The old consumer and queued wake helpers are now retired.
                # Prepare the actual domain with the candidate core before
                # exposing its native package or committing its config tuple.
                # The Hook never initializes or migrates persistent state.
                store_ready = json.loads(self.command(
                    [candidate["python"], Path(candidate["plugin"]) / "scripts/capture_config.py", "prepare-store"],
                    data=json.dumps(read(adapter["engine_config"])), on_failure=store_failure))
                if store_ready != {"status": "ready"}:
                    raise RuntimeError("candidate knowledge store preparation did not confirm readiness")
                market = self.root / "marketplace"
                plugin_link = market / "plugins/mindie-agent"
                previous_native = self.native_plugin_entry()
                journal = dict(
                    adapter=adapter,
                    candidate=candidate["revision"],
                    candidate_generation=candidate,
                    marketplace=existing["root"] if existing else None,
                    link=str(plugin_link.resolve()) if plugin_link.exists() else None,
                    previous_version=(previous_native or {}).get("version"),
                )
                atomic(self.root / "transaction.json", journal)
                atomic(
                    market / ".agents/plugins/marketplace.json",
                    dict(
                        name="mindie-agent",
                        plugins=[
                            dict(
                                name="mindie-agent",
                                source=dict(
                                    source="local", path="./plugins/mindie-agent"
                                ),
                                policy=dict(
                                    installation="AVAILABLE",
                                    authentication="ON_INSTALL",
                                ),
                                category="Productivity",
                            )
                        ],
                    ),
                )
                link(plugin_link, candidate["plugin"])
                final_proven = False
                self.register(market)
                self.command(
                    [
                        self.settings["codex"],
                        "plugin",
                        "add",
                        "mindie-agent@mindie-agent",
                        "--json",
                    ]
                )
                    # The add receipt is not proof: retained caches compete in
                # native discovery. Verify the actual resolved version AND
                # its bytes or roll back instead of reporting a fake
                # installed state.
                self.verify_native(candidate["version"], candidate["plugin"])
                engine = read(adapter["engine_config"])
                # One committed generation: worker, transcript parser and
                # interpreter move together; the neutral admission store and
                # any legacy activation key are adapter-scope, not per
                # generation. The old session_activation alias is removed
                # instead of kept as a second name.
                engine.pop("session_activation", None)
                engine.pop("agent_command", None)
                # Metadata policy belongs to the installed adapter. Capture
                # config replaces retired user-selected worker arguments.
                engine.update(
                    transcript_adapter=str(
                        Path(candidate["plugin"]) / "scripts/codex_transcript.py"
                    ),
                )
                engine.update(capture_config)
                declaration, _ = product_contract.product(candidate["source"])
                publication = declaration["publication"]
                engine["product_validation"] = candidate["validation"]
                for feed in engine.get("feeds", []):
                    if feed.get("repository") == publication["repository"]:
                        feed["contract_sha256"] = publication["contract_sha256"]
                admission_path = engine.get("admission_path")
                if not isinstance(admission_path, str):
                    admission_path = str(
                        self.config.with_name(self.config.stem + ".admission.sqlite3")
                    )
                    engine["admission_path"] = admission_path
                engine_path = Path(candidate["plugin"]).parent / "engine.json"
                atomic(engine_path, engine)
                atomic(
                    self.config,
                    dict(
                        adapter,
                        python=candidate["python"],
                        engine_config=str(engine_path),
                        runtime_scripts=str(Path(candidate["plugin"]) / "scripts"),
                        admission_path=admission_path,
                        product_validation=candidate["validation"],
                    ),
                )
                try:
                    # Upgrade boundary: converge the community path and the
                    # consent authority once. A deferred migration is recorded
                    # truthfully and retried at the next entry attach; it
                    # never fails the completed update.
                    import consent as _consent
                    import sharing as _sharing

                    _consent.migrate_legacy(self.config)
                    _sharing.migrate_community_path(self.config)
                except Exception as migration_exc:
                    self.state["settings_migration"] = (
                        "deferred: " + type(migration_exc).__name__
                    )
                self.project_publication(declaration)
                committed_journal = read(self.root / "transaction.json")
                committed_journal["generation_committed"] = True
                atomic(self.root / "transaction.json", committed_journal)
                final_proven = True
                installed = True
                self._generation_committed = True
                result = self.save(
                    "installed",
                    error=None,
                    original_install_error=None,
                    rollback_error=None,
                    current=candidate,
                    candidate=candidate["revision"],
                    activation="task authorization preserved; refreshed host definitions load in new tasks; changed hooks require native trust review",
                )
                (self.root / "transaction.json").unlink()
                installed = True
                try:
                    self.publish_stable_launcher()
                except Exception as exc:
                    result = self.save("partial", launcher_error=dict(
                        stage="publish_stable_launcher", error_type=type(exc).__name__,
                        generation_committed=True, automatic_retry=False))
                else:
                    self.state.pop("launcher_error", None)
                return result
            except Exception as install_exc:
                if installed:
                    # Business publication completed; a later persistence or
                    # cleanup error must not replay installation or rollback.
                    raise
                final_proven = False
                try:
                    self.recover()
                except Exception as rollback_exc:
                    combined = InstallRollbackError(install_exc, rollback_exc)
                    install_exc.add_note("Rollback also failed: " + type(rollback_exc).__name__)
                    try:
                        self.save("update_failed", error=str(combined),
                                  original_install_error=combined.original_install_error,
                                  rollback_error=combined.rollback_error)
                    except Exception as record_exc:
                        combined.add_note("Failure-state persistence also failed: " + type(record_exc).__name__)
                    raise combined from install_exc
                final_proven = True
                try:
                    self.publish_stable_launcher()
                except Exception as launcher_exc:
                    install_exc.add_note("Restored launcher publication also failed: " + type(launcher_exc).__name__)
                    self.state["launcher_error"] = dict(stage="publish_restored_launcher",
                        error_type=type(launcher_exc).__name__, generation_committed=False)
                raise
            finally:
                primary = sys.exc_info()[1]
                try:
                    self.restore_service(final_proven, handoff, candidate)
                    if installed:
                        self.save("installed")
                except Exception as secondary:
                    if primary is None:
                        raise
                    # A restore/save failure cannot replace the failed native
                    # installation. Keep both facts in the returned state.
                    self.state["operation_error"] = dict(stage="install",
                        error_type=type(primary).__name__, automatic_retry=False)
                    primary.add_note("Service restoration bookkeeping also failed: " + type(secondary).__name__)

    def maintain_diagnostics(self):
        """One offline maintenance call plus a bounded handoff request for an
        already enabled AND healthy-running reporter (--update-running); it
        never starts a disabled, absent, or crashed service. Does not install
        a runtime or report itself. The actual JSON result (including an
        aggregate status=degraded from a failed/conflicting handoff) is
        returned; exit 1 yields normal JSON via allowed_returncodes=(0, 1).
        """
        try:
            adapter = read(self.config)
            python = adapter.get("python") if isinstance(adapter, dict) else None
            if not isinstance(python, str) or not python or not os.path.isfile(python):
                return {"status": "deferred", "error_type": "missing_runtime"}
            import consent as _consent

            reporting_enabled = _consent.load(self.config).get("reporting") == "enabled"
            # The diagnostics component owns maintenance scheduling. A host
            # check never kills it because an unrelated plugin step was slow.
            command = [python, "-m", "mindie_diagnostics.cli", "reporting", "maintain"]
            # Local retention also runs when reporting is off. Only the saved
            # reporting choice permits handing off an existing reporter.
            if reporting_enabled:
                command.append("--update-running")
            output = self.command(
                command,
                allowed_returncodes=(0, 1),
            )
            if len(output.encode()) > 1024 * 1024:
                return {"status": "unavailable", "error_type": "ValueError"}
            payload = json.loads(output)
            if not isinstance(payload, dict):
                return {"status": "unavailable", "error_type": "ValueError"}
            return payload
        except FileNotFoundError:
            return {"status": "deferred", "error_type": "missing_runtime"}
        except Exception as exc:
            return {"status": "unavailable", "error_type": type(exc).__name__}

    def check(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._state_write_error = None
        self._generation_committed = False
        try:
            with file_lock(self.root / "checker.lock", exclusive=True):
                self.state = read(self.state_path, {})
                result = self._check_locked()
                retention = self.collect_generations()
                self.state["retention"] = retention
                self.save(self.state.get("status", "unknown"))
        except Exception as exc:
            if exc is not self._state_write_error:
                if isinstance(exc, BlockingIOError):
                    return dict(status="already_running")
                raise
            # The original write error reaches the caller alongside any
            # already-proven publication/restoration facts. No second save.
            self.state["status"] = "partial" if self._generation_committed else "update_failed"
            self.state["error"] = self.state.get("error") or "update state persistence failed: " + type(exc).__name__
            result = self.state
            retention = dict(status="deferred", reason="state_persistence_failed")
        maintenance = self.maintain_diagnostics()
        try:
            atomic(self.root / "diagnostics-maintenance.json", maintenance)
        except (OSError, ValueError) as exc:
            maintenance = dict(maintenance, status="unavailable",
                               error_type=type(exc).__name__, stage="persist_maintenance")
        return dict(result, retention=retention, diagnostics=maintenance)

    def publish_runtime_launcher(self, candidate):
        """Stable, reviewed entry bytes; native tasks never name old env paths."""
        scripts = Path(candidate["plugin"]) / "scripts"
        for name in ("update_lock.py", "runtime_launcher.py", "diagnostic_support.py",
                     "diagnostic_fallback.py", "agent_diagnostics.py", "diagnostic-build.json"):
            atomic_text(self.root / name, (scripts / name).read_text(encoding="utf-8"))

    def collect_generations(self, *, exclusive=False):
        """Reclaim only owned, unreachable generations with no process lease.

        Old untracked installations are reported, never inferred dead from
        age. Their native tasks may still contain an absolute entry path.
        A fresh v2 installation needs no retained native-cache copies.
        """
        result = dict(status="complete", removed=[], kept=[], untracked=[])
        try:
            with update_lock(self.config, exclusive=exclusive):
                # GC must never interpret a lost state file as first use.
                state = read(self.state_path)
                if not isinstance(state, dict):
                    raise ValueError("generation state is not an object")
                current = state.get("current")
                if current is not None and not isinstance(current, dict):
                    raise ValueError("invalid committed generation")
                keep = {state.get("candidate"), (current or {}).get("revision")}
                adapter = read(self.config)
                if not isinstance(adapter, dict) or not isinstance(adapter.get("runtime_scripts"), str):
                    raise ValueError("runtime pointer is missing or invalid")
                scripts = Path(adapter["runtime_scripts"]).resolve(strict=True)
                generations = (self.root / "generations").resolve()
                pointed_generation = scripts.parent.parent
                if pointed_generation.parent == generations:
                    if scripts != pointed_generation / "plugin/scripts":
                        raise ValueError("invalid runtime pointer layout")
                    keep.add(pointed_generation.name)
                    if current and current.get("revision") != pointed_generation.name:
                        return dict(result, status="failed", stage="generation_cleanup",
                                    error_type="StateMismatch", error="runtime pointer differs from committed state")
                journal_path = self.root / "transaction.json"
                if journal_path.exists():
                    read(journal_path)  # Malformed recovery state must stay visible.
                    # Interrupted transactions keep all generations until
                    # reconciliation establishes the committed state.
                    return dict(result, status="deferred", reason="transaction_pending")
                executing = Path(__file__).resolve().parents[2]
                if executing.parent == (self.root / "generations").resolve():
                    keep.add(executing.name)
                directory = self.root / "generations"
                locks = self.root / "generation-locks"
                locks.mkdir(exist_ok=True)
                for generation in sorted(directory.iterdir()) if directory.exists() else []:
                    if generation.is_symlink() or not generation.is_dir():
                        result["untracked"].append(generation.name)
                        continue
                    marker = read(generation / "ownership.json", {})
                    if marker != {"schema": "mindie-runtime-generation/2", "revision": generation.name}:
                        result["untracked"].append(generation.name)
                        continue
                    if generation.name in keep:
                        result["kept"].append(generation.name)
                        continue
                    try:
                        with file_lock(locks / (generation.name + ".lock"), exclusive=True):
                            remove_owned_tree(generation)
                            result["removed"].append(generation.name)
                        (locks / (generation.name + ".lock")).unlink(missing_ok=True)
                    except BlockingIOError:
                        result["kept"].append(generation.name)
                # These are updater-made duplicates, not host-managed caches.
                backup = self.root / "retained-caches"
                if backup.exists() and not backup.is_symlink():
                    remove_owned_tree(backup)
                attempts = state.get("attempts")
                if isinstance(attempts, dict):
                    protected = set(result["kept"]) | set(result["untracked"]) | keep
                    self.state["attempts"] = {key: value for key, value in attempts.items() if key in protected}
        except BlockingIOError:
            return dict(result, status="deferred", reason="runtime_switch_in_progress")
        except (OSError, ValueError, TypeError) as exc:
            return dict(result, status="failed", stage="generation_cleanup", error_type=type(exc).__name__,
                        error=str(exc)[:240])
        if result["untracked"]:
            result["status"] = "untracked_retained"
        return result

    def check_knowledge(self):
        """Model-free knowledge sync on the same 300 s schedule.

        Independent of plugin build state and of the community sharing switch:
        sharing off still receives published updates. One bounded call into the
        knowledge core, which owns its own per-candidate attempt persistence;
        failures are recorded under knowledge_* keys and never mask or cancel
        the plugin check that follows.
        """
        with update_lock(self.config):
            adapter = read(self.config)
            output = self.command(
                [
                    adapter["python"],
                    "-m",
                    "mindie_knowledge.loop.cli",
                    "sync",
                    "--config",
                    adapter["engine_config"],
                ],
            )
        aggregate, rows, summary = fold_feed_results(output)
        self.save(
            self.state.get("status", "unknown"),
            knowledge_status=aggregate,
            knowledge_feeds=len(rows),
            knowledge_results=rows,
            knowledge_error=summary,
            knowledge_checked_at=time.time(),
        )

    def _check_locked(self):
            knowledge_error = None
            try:
                self.check_knowledge()
            except Exception as exc:
                knowledge_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if knowledge_error:
                self.save(
                    self.state.get("status", "unknown"),
                    knowledge_status="sync_failed",
                    knowledge_error=knowledge_error,
                    knowledge_results=None,
                    knowledge_feeds=None,
                    knowledge_checked_at=time.time(),
                )
            try:
                return self._check_plugin()
            except Exception as exc:
                # The plugin check owns its failure states; this guard only
                # keeps an unexpected crash from hiding the knowledge result.
                diagnostic = None
                try:
                    import diagnostic_support
                    reported = diagnostic_support.failure(
                        "update", "internal", "internal", exception=exc
                    )
                    if isinstance(reported, dict):
                        diagnostic = {
                            key: reported[key]
                            for key in ("incident_id", "logging_failed", "recorded")
                            if key in reported
                        }
                except Exception:
                    diagnostic = None
                extra = {}
                if diagnostic:
                    extra["diagnostic"] = diagnostic
                return self.save(
                    "update_failed",
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                    **extra,
                )

    def publish_stable_launcher(self):
        """Copy the committed generation's dispatcher onto the stable launcher.

        The scheduler keeps the same launcher path, so this does not
        re-register it. Hook and loaded plugin files are not rewritten.
        A missing current generation is left alone; an invalid one fails.
        """
        current = self.state.get("current")
        if current is None:
            return
        if not isinstance(current, dict):
            raise Incompatible("invalid current generation")
        plugin = current.get("plugin")
        if plugin is None and not current:
            return
        if not isinstance(plugin, str) or not plugin or not Path(plugin).is_absolute():
            raise Incompatible("invalid current generation")
        try:
            plugin_path = Path(plugin).resolve()
            plugin_path.relative_to((self.root / "generations").resolve())
        except (OSError, ValueError):
            raise Incompatible("invalid current generation") from None
        source = plugin_path / "scripts" / "update_launcher.py"
        if not source.is_file():
            raise Incompatible("invalid current generation")
        launcher = self.root / "launcher.py"
        try:
            if launcher.is_file() and launcher.read_bytes() == source.read_bytes():
                return
        except OSError:
            pass
        temporary = self.root / "launcher.next"
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, launcher)
        finally:
            temporary.unlink(missing_ok=True)

    def _startup_current(self, *, recover):
        """Publish a clean committed launcher. Recover a journal only when due."""
        if recover and (self.root / "transaction.json").exists():
            with update_lock(self.config, exclusive=True):
                self.recover()
        self.publish_stable_launcher()
        self.state.pop("launcher_error", None)

    def _note_resolve_failure(self, exc):
        failures = self.state.get("check_failures", 0) + 1
        if not isinstance(self.state.get("check_failure_at"), (int, float)) or isinstance(
            self.state.get("check_failure_at"), bool
        ):
            self.state["check_failure_at"] = time.time()
        category = _failure_category(exc)
        fields = dict(check_failures=failures)
        if category in _RETRYABLE or category in _ACTIONABLE or category in _QUARANTINE:
            delay = _retry_delay(failures, getattr(exc, "retry_after", None))
            nxt = time.time() + delay
            fields.update(
                status="action_required" if category in _ACTIONABLE else "check_failed",
                failure_class=category,
                error=_STATIC_FAILURE[category],
                next_retry_at=nxt,
                next_check=nxt,
            )
        else:
            self.state.pop("failure_class", None)
            self.state.pop("next_retry_at", None)
            fields.update(
                status="check_failed",
                error=str(exc)[:240],
                next_check=time.time() + (3600 if failures >= ATTEMPTS else INTERVAL),
            )
        return self.save(**fields)

    def _remember_attempt(self, record, exc):
        category = _failure_category(exc)
        if not isinstance(record.get("first_failure_at"), (int, float)) or isinstance(
            record.get("first_failure_at"), bool
        ):
            record["first_failure_at"] = time.time()
        history = record.get("history")
        if not isinstance(history, list):
            history = []
        history.append({"at": time.time(), "class": category or "unknown"})
        del history[:-8]
        record["history"] = history
        if category in _QUARANTINE:
            record["failure_class"] = category
            record["quarantined"] = True
            record["reason"] = _STATIC_FAILURE[category]
            record.pop("next_retry_at", None)
            self.state.pop("next_retry_at", None)
            self.state["failure_class"] = category
            return "quarantine"
        if category in _RETRYABLE or category in _ACTIONABLE:
            record["failure_class"] = category
            record["last_error"] = _STATIC_FAILURE[category]
            record.pop("quarantined", None)
            record["next_retry_at"] = time.time() + _retry_delay(
                record.get("count", 1), getattr(exc, "retry_after", None)
            )
            self.state["next_retry_at"] = record["next_retry_at"]
            self.state["failure_class"] = category
            return "retry"
        record.pop("failure_class", None)
        record.pop("next_retry_at", None)
        record.pop("quarantined", None)
        self.state.pop("next_retry_at", None)
        self.state.pop("failure_class", None)
        record["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return "unknown"

    def _clear_attempt_active(self, sha):
        attempts = self.state.get("attempts")
        record = attempts.get(sha) if isinstance(attempts, dict) else None
        if isinstance(record, dict):
            record.pop("next_retry_at", None)
            record.pop("failure_class", None)
        self.state.pop("next_retry_at", None)
        self.state.pop("failure_class", None)

    def _check_plugin(self):
        due = self.state.get("next_check", 0) <= time.time()
        journal = (self.root / "transaction.json").exists()
        if journal and not due:
            return self.state
        try:
            self._startup_current(recover=due)
        except Exception as exc:
            return self._note_resolve_failure(exc)
        if not due:
            return self.state
        try:
            sha = self.resolve()
        except Exception as exc:
            return self._note_resolve_failure(exc)
        self.state.update(
            check_failures=0, next_check=time.time() + INTERVAL, error=None
        )
        self.state.pop("failure_class", None)
        self.state.pop("next_retry_at", None)
        if self.state.get("current", {}).get("revision") == sha:
            self._clear_attempt_active(sha)
            return self.save("up_to_date", error=None)
        attempts = self.state.setdefault("attempts", {})
        record = attempts.get(sha)
        if not isinstance(record, dict):
            record = {"count": 0}
            attempts[sha] = record
        if not isinstance(record.get("count"), int) or isinstance(record.get("count"), bool):
            record["count"] = 0
        klass = record.get("failure_class")
        if (
            record.get("incompatible")
            or record.get("quarantined")
            or klass in _QUARANTINE
        ):
            return self.save(
                "waiting_for_compatible_source",
                candidate=sha,
                error=record.get("reason")
                or record.get("last_error")
                or "source lacks required compatibility contract",
            )
        if klass in _RETRYABLE or klass in _ACTIONABLE:
            due = record.get("next_retry_at")
            if isinstance(due, (int, float)) and not isinstance(due, bool) and due > time.time():
                return self.save(
                    "action_required" if klass in _ACTIONABLE else "update_failed",
                    candidate=sha,
                    error=_STATIC_FAILURE.get(klass, record.get("last_error")),
                    next_retry_at=due,
                    failure_class=klass,
                )
        elif record.get("count", 0) >= ATTEMPTS:
            # Legacy or unknown exhaustion stays visible. Do not relabel it.
            return self.save(
                "attempts_exhausted",
                candidate=sha,
                error=record.get("last_error", "update attempt limit reached"),
            )
        # A crash consumes an attempt. A normal idle deferral refunds it.
        record["count"] += 1
        record.pop("next_retry_at", None)
        self.state.pop("next_retry_at", None)
        self.save("preparing", candidate=sha)  # Reserve before doing fallible work.
        try:
            candidate = self.prepare(sha)
            result = self.install(candidate)
            if result["status"] == "waiting_for_idle":
                record["count"] -= 1
                return self.save("waiting_for_idle", candidate=sha)
            if result.get("status") in {"installed", "up_to_date", "degraded"}:
                self._clear_attempt_active(sha)
                return self.save(result["status"], error=None)
            return result
        except BlockingIOError:
            record["count"] -= 1
            return self.save("waiting_for_idle", candidate=sha)
        except Incompatible as exc:
            record["incompatible"] = True
            record["reason"] = str(exc)
            record.pop("next_retry_at", None)
            if not isinstance(record.get("first_failure_at"), (int, float)):
                record["first_failure_at"] = time.time()
            return self.save(
                "waiting_for_compatible_source", error=str(exc), candidate=sha
            )
        except Exception as exc:
            if exc is getattr(self, "_state_write_error", None):
                raise
            kind = self._remember_attempt(record, exc)
            if kind == "quarantine":
                return self.save(
                    "waiting_for_compatible_source",
                    error=record.get("reason"),
                    candidate=sha,
                    failure_class=record.get("failure_class"),
                )
            if kind == "retry":
                return self.save(
                    "action_required"
                    if record.get("failure_class") in _ACTIONABLE
                    else "update_failed",
                    error=record.get("last_error"),
                    candidate=sha,
                    next_retry_at=record.get("next_retry_at"),
                    failure_class=record.get("failure_class"),
                )
            return self.save(
                "update_failed",
                error=record.get("last_error"),
                candidate=sha,
            )


def _native_run(updater, argv, timeout=None):
    """Run native scheduler control and retain its actual exit status.

    No default execution deadline. Cleanup callers may explicitly bound their
    teardown/readback window; failures remain distinct from proven absence.
    """
    completed = run([str(arg) for arg in argv], "", timeout=timeout,
                    allowed_returncodes=None)
    completed.checked_stdout()
    return completed.returncode, completed.stdout, completed.stderr


def _launchd_state(updater, label, timeout=None):
    """Actual launchd state for the exact service target.

    "absent" ONLY on positive missing-service evidence (launchctl print
    exit 113); permission, manager, and parse failures are "unknown",
    never absence.
    """
    try:
        code, _, stderr = _native_run(
            updater, ["launchctl", "print", f"gui/{os.getuid()}/{label}"], timeout)
    except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return "unknown", f"{type(exc).__name__}: {exc}"[:240]
    if code == 0:
        return "loaded", ""
    if code == 113:
        return "absent", ""
    return "unknown", (stderr or "").strip()[:240] or f"launchctl print exited {code}"


def _task_state(updater, task, timeout=None):
    """Exact scheduled-task state via structured PowerShell enumeration.

    ErrorAction Stop makes a manager error exit nonzero; a successful
    enumeration with zero exact TaskPath/TaskName matches proves absence.
    Localized arbitrary error text is never treated as absence.
    """
    escaped = task.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; "
        "$m = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { "
        f"$_.TaskName -eq '{escaped}' -and $_.TaskPath -eq '\\' }}); "
        "Write-Output $m.Count"
    )
    try:
        code, stdout, stderr = _native_run(
            updater,
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout)
    except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return "unknown", f"{type(exc).__name__}: {exc}"[:240]
    if code != 0:
        return "unknown", (stderr or "").strip()[:240] or f"powershell exited {code}"
    try:
        count = int((stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return "unknown", "unparseable task enumeration"
    return ("present" if count else "absent"), ""


def _systemd_state(updater, timer=SYSTEMD_TIMER, timeout=None):
    """Read exact user timer state; manager or parse faults stay unknown."""
    try:
        code, stdout, stderr = _native_run(
            updater,
            [
                "systemctl", "--user", "show", timer,
                "--property=LoadState", "--property=ActiveState",
                "--property=UnitFileState", "--no-pager",
            ],
            timeout,
        )
    except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return "unknown", f"{type(exc).__name__}: {exc}"[:240]
    if code != 0:
        return "unknown", (stderr or "").strip()[:240] or f"systemctl exited {code}"
    props = {}
    for line in (stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"LoadState", "ActiveState", "UnitFileState"}:
            props[key] = value.strip()
    if set(props) != {"LoadState", "ActiveState", "UnitFileState"}:
        return "unknown", "unparseable systemd timer properties"
    if props["LoadState"] == "not-found":
        return "absent", ""
    if props["LoadState"] != "loaded":
        return "unknown", "unexpected LoadState=" + props["LoadState"]
    detail = ";".join(f"{key}={props[key]}" for key in sorted(props))
    if (
        props["ActiveState"] == "active"
        and props["UnitFileState"] in {"enabled", "enabled-runtime"}
    ):
        return "active", detail
    return "present", detail


def _systemd_property(detail, name):
    for item in detail.split(";"):
        key, sep, value = item.partition("=")
        if sep and key == name:
            return value
    return None


def _systemd_quote(value):
    value = str(value)
    if any(character in value for character in ("\0", "\n", "\r")):
        raise ValueError("systemd command paths cannot contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _systemd_user_root(schedule_root=None):
    if schedule_root is not None:
        return Path(schedule_root).expanduser().absolute()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return (base / "systemd/user").absolute()


def _validate_systemd_paths(timer_path, service_path):
    for label, path in (("timer", timer_path), ("service", service_path)):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError(
                f"refusing to replace a non-file systemd {label} path"
            )


def _schedule_preflight(updater):
    """Prove this user's scheduler is reachable before registering a plugin."""
    if sys.platform == "darwin":
        state, detail = _launchd_state(updater, LABEL)
    elif os.name == "nt":
        state, detail = _task_state(updater, WIN_TASK)
    elif sys.platform.startswith("linux"):
        state, detail = _systemd_state(updater)
    else:
        raise ValueError(
            "automatic scheduling is supported on macOS, Windows, and Linux "
            "with a per-user systemd manager; use --schedule manual elsewhere"
        )
    if state == "unknown":
        raise RuntimeError(
            "the per-user update scheduler is unavailable or its state is "
            f"unproven ({detail or 'query failed'}); rerun with --schedule manual"
        )


def _schedule_systemd_enable(updater, launcher, settings_path, *, schedule_root=None,
                             timer=SYSTEMD_TIMER, service=SYSTEMD_SERVICE):
    root = _systemd_user_root(schedule_root)
    timer_path = root / timer
    service_path = root / service
    _validate_systemd_paths(timer_path, service_path)
    state, detail = _systemd_state(updater, timer)
    if state == "unknown":
        raise RuntimeError(f"systemd timer state unproven: {detail}")
    service_text = "\n".join((
        "[Unit]",
        "Description=MindIE Agent plugin update check",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=" + " ".join(_systemd_quote(arg) for arg in (
            updater.settings["python"], str(launcher), str(settings_path),
        )),
        "",
    ))
    timer_text = "\n".join((
        "[Unit]",
        "Description=Check for MindIE Agent plugin updates every five minutes",
        "",
        "[Timer]",
        "OnBootSec=2min",
        "OnUnitActiveSec=5min",
        f"Unit={service}",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ))
    atomic_text(service_path, service_text)
    atomic_text(timer_path, timer_text)
    code, _, stderr = _native_run(updater, ["systemctl", "--user", "daemon-reload"])
    if code:
        raise RuntimeError("systemd daemon-reload failed: " + (stderr or "").strip()[:200])
    code, _, stderr = _native_run(
        updater, ["systemctl", "--user", "enable", "--now", timer]
    )
    state, detail = _systemd_state(updater, timer)
    if state != "active":
        raise RuntimeError(
            "systemd timer registration unproven"
            + (f" ({detail or (stderr or '').strip()[:200]})" if detail or stderr else "")
        )
    return f"systemd user timer: {timer}"


def schedule_enable(updater, launcher, settings_path, *, schedule_root=None):
    """Register the periodic check with the platform scheduler."""
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        plist.parent.mkdir(parents=True, exist_ok=True)
        content = dict(
            Label=LABEL,
            ProgramArguments=[
                sys.executable,
                str(launcher),
                str(settings_path),
            ],
            RunAtLoad=True,
            StartInterval=INTERVAL,
            ProcessType="Background",
            ExitTimeOut=5,
            EnvironmentVariables={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        with plist.open("wb") as stream:
            plistlib.dump(content, stream)
        try:
            updater.command(
                ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]
            )
        except RuntimeError:
            pass
        else:
            updater.command(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"]
            )
        updater.command(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", plist]
        )
        return str(plist)
    if os.name == "nt":
        # Windows (unverified on real hardware): a per-user scheduled task.
        updater.command(
            [
                "schtasks",
                "/Create",
                "/TN",
                WIN_TASK,
                "/SC",
                "MINUTE",
                "/MO",
                str(INTERVAL // 60),
                "/TR",
                f'"{sys.executable}" "{launcher}" "{settings_path}"',
                "/F",
            ],
        )
        state, detail = _task_state(updater, WIN_TASK)
        if state != "present":
            raise RuntimeError(
                "scheduled task registration unproven"
                + (f" ({detail})" if detail else "")
            )
        return WIN_TASK
    if sys.platform.startswith("linux"):
        return _schedule_systemd_enable(
            updater, launcher, settings_path, schedule_root=schedule_root
        )
    raise ValueError("automatic scheduling requires a supported per-user scheduler")


def schedule_disable(updater, *, label=None, plist_path=None, schedule_root=None,
                     task=None, systemd_root=None, timer_path=None,
                     service_path=None, timer=None, service=None):
    """Remove the platform schedule; installed plugin and state stay in place.

    Truthful removal of ONLY this updater's exact owned target: query real
    scheduler state before any mutation; only positively identified absence
    (launchctl print exit 113, or a structured PowerShell enumeration with
    zero exact TaskPath/TaskName matches) is idempotent success. A loaded
    service gets exactly one bootout/unregister by service target plus a
    bounded absence readback — never a second mutation; an uncertain
    removal result can still end in success when the readback proves
    absence. Permission, manager, and parse failures raise; they are never
    treated as absence. The plist is unlinked only after absence is
    established. Public callable defaults are unchanged; the keyword-only
    explicit label/plist-path/schedule-root/task exist for isolated native
    acceptance.
    """
    if updater.settings.get("schedule_mode") == "manual":
        return
    if sys.platform == "darwin":
        label = label or LABEL
        target = f"gui/{os.getuid()}/{label}"
        state, detail = _launchd_state(updater, label)
        if state == "unknown":
            raise RuntimeError(f"launchd state unproven: {detail or 'query failed'}")
        if state == "loaded":
            removal_detail = ""
            try:
                code, _, stderr = _native_run(
                    updater, ["launchctl", "bootout", target], 10)
                if code:
                    removal_detail = (stderr or "").strip()[:240]
            except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
                removal_detail = f"{type(exc).__name__}: {exc}"[:240]
            cutoff = time.monotonic() + 3.0
            while state != "absent" and time.monotonic() < cutoff:
                time.sleep(0.2)
                left = cutoff - time.monotonic()
                if left <= 0:
                    break
                state, detail = _launchd_state(updater, label,
                                               timeout=min(5.0, left))
            if state != "absent":
                problem = "still loaded" if state == "loaded" else "state unknown"
                info = detail or removal_detail
                raise RuntimeError(
                    f"schedule removal unproven: service {problem}"
                    + (f" ({info})" if info else ""))
        if plist_path is not None:
            plist = Path(plist_path).expanduser().absolute()
        else:
            root = (Path(schedule_root).expanduser().absolute() if schedule_root
                    else Path.home() / "Library/LaunchAgents")
            plist = root / (label + ".plist")
        plist.unlink(missing_ok=True)
        return
    if os.name == "nt":
        # Windows (unverified on real hardware).
        task = task or WIN_TASK
        state, detail = _task_state(updater, task)
        if state == "unknown":
            raise RuntimeError(
                f"scheduled task state unproven: {detail or 'query failed'}")
        if state == "present":
            escaped = task.replace("'", "''")
            script = (f"Unregister-ScheduledTask -TaskName '{escaped}' "
                      "-TaskPath '\\' -Confirm:$false -ErrorAction Stop")
            try:
                code, _, stderr = _native_run(
                    updater,
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     script],
                    20)
                if code:
                    detail = (stderr or "").strip()[:240]
            except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
                detail = f"{type(exc).__name__}: {exc}"[:240]
            cutoff = time.monotonic() + 3.0
            while state != "absent" and time.monotonic() < cutoff:
                time.sleep(0.2)
                left = cutoff - time.monotonic()
                if left <= 0:
                    break
                state, query_detail = _task_state(updater, task,
                                                  timeout=min(10.0, left))
                if state != "present":
                    detail = query_detail or detail
            if state != "absent":
                problem = "still present" if state == "present" else "state unknown"
                raise RuntimeError(
                    f"schedule removal unproven: task {problem}"
                    + (f" ({detail})" if detail else ""))
        return
    if sys.platform.startswith("linux"):
        timer = timer or SYSTEMD_TIMER
        service = service or SYSTEMD_SERVICE
        root = _systemd_user_root(systemd_root or schedule_root)
        timer_file = Path(timer_path).expanduser().absolute() if timer_path else root / timer
        service_file = Path(service_path).expanduser().absolute() if service_path else root / service
        _validate_systemd_paths(timer_file, service_file)
        state, detail = _systemd_state(updater, timer)
        if state == "unknown":
            raise RuntimeError(
                f"systemd timer state unproven: {detail or 'query failed'}"
            )
        if state != "absent":
            removal_detail = ""
            try:
                code, _, stderr = _native_run(
                    updater, ["systemctl", "--user", "disable", "--now", timer], 15
                )
                if code:
                    removal_detail = (stderr or "").strip()[:240]
            except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
                removal_detail = f"{type(exc).__name__}: {exc}"[:240]
            state, detail = _systemd_state(updater, timer)
            if state == "unknown":
                raise RuntimeError(
                    "systemd timer removal unproven: "
                    + (detail or removal_detail or "query failed")
                )
            still_enabled = _systemd_property(detail, "UnitFileState") in {
                "enabled", "enabled-runtime",
            }
            still_active = _systemd_property(detail, "ActiveState") == "active"
            if state == "active" or still_enabled or still_active:
                raise RuntimeError(
                    "schedule removal unproven: systemd timer remains active or "
                    "enabled"
                    + (f" ({detail or removal_detail})" if detail or removal_detail else "")
                )
        # Deletion follows a readback proving that the timer is inactive and
        # disabled. These are exactly our per-user unit filenames.
        timer_file.unlink(missing_ok=True)
        service_file.unlink(missing_ok=True)
        code, _, stderr = _native_run(
            updater, ["systemctl", "--user", "daemon-reload"]
        )
        if code:
            raise RuntimeError(
                "systemd unit reload after removal failed: "
                + (stderr or "").strip()[:200]
            )
        state, detail = _systemd_state(updater, timer)
        if state != "absent":
            raise RuntimeError(
                "schedule removal unproven: systemd timer "
                + ("still present" if state != "unknown" else "state unknown")
                + (f" ({detail})" if detail else "")
            )
        return
    raise ValueError("automatic scheduling requires a supported per-user scheduler")


def enable(args):
    source = args.source_root.expanduser().absolute()
    root = args.root.expanduser().absolute()
    settings_path = args.settings.expanduser().absolute()
    schedule_mode = getattr(args, "schedule", "auto")
    if schedule_mode not in {"auto", "manual"}:
        raise ValueError("schedule mode must be auto or manual")
    settings = read(settings_path, {})
    if settings:
        root = Path(settings["root"])
        settings["channel"] = args.channel
    else:
        settings = dict(
            root=str(root),
            adapter_config=str(config_path()),
            repository=REPOSITORY,
            channel=args.channel,
            python=sys.executable,
            codex=shutil.which("codex"),
            uv=shutil.which("uv"),
            codex_home=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        )
    settings["schedule_mode"] = schedule_mode
    if not settings["codex"] or not settings["uv"]:
        raise ValueError("codex and uv are required")
    root.mkdir(parents=True, exist_ok=True)
    atomic(settings_path, settings)
    updater = Updater(settings_path)
    if schedule_mode == "auto":
        _schedule_preflight(updater)
    if (root / "transaction.json").exists():
        with update_lock(updater.config, exclusive=True):
            updater.recover()
    candidate = updater.state.get("current")
    if not candidate:
        updater.validate_source(source)
        validation = updater.probe_runtime(
            read(updater.config)["python"],
            source / "plugins/mindie-agent/scripts",
        )
        # Preserve local safety fixes without altering a dirty checkout or inventing a remote revision.
        generation = (
            root
            / "generations"
            / ("local-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
        )
        generation.mkdir(parents=True)
        candidate = updater.package(
            generation, source, read(updater.config)["python"], generation.name, validation
        )
        result = updater.install(candidate)
        if result["status"] == "degraded":
            raise RuntimeError("plugin installed but service restoration needs attention")
        if result["status"] != "installed":
            raise RuntimeError(
                "wait for in-flight MindIE calls before enabling updater"
            )
    controller = root / "controller"
    shutil.copytree(
        Path(candidate["plugin"]) / "scripts", controller, dirs_exist_ok=True
    )
    launcher = root / "launcher.py"
    temporary_launcher = root / "launcher.next"
    shutil.copy2(Path(__file__).with_name("update_launcher.py"), temporary_launcher)
    os.replace(temporary_launcher, launcher)
    if schedule_mode == "manual":
        registration = dict(
            mode="manual",
            registered=False,
            check_command=[settings["python"], str(launcher), str(settings_path)],
        )
        result_status = "manual"
    else:
        target = schedule_enable(updater, launcher, settings_path)
        registration = dict(mode="automatic", registered=True, target=target)
        result_status = "enabled"
    settings["schedule"] = registration
    atomic(settings_path, settings)
    return dict(
        status=result_status,
        channel=args.channel,
        settings=str(settings_path),
        schedule=registration,
    )


def schedule_status(updater):
    """Report persisted intent and a readback of automatic scheduler state."""
    mode = updater.settings.get("schedule_mode", "auto")
    if mode == "manual":
        return dict(mode="manual", registered=False)
    if sys.platform == "darwin":
        state, detail = _launchd_state(updater, LABEL)
    elif os.name == "nt":
        state, detail = _task_state(updater, WIN_TASK)
    elif sys.platform.startswith("linux"):
        state, detail = _systemd_state(updater)
    else:
        state, detail = "unknown", "no supported per-user scheduler"
    registered = state in {"loaded", "active"}
    if state == "present":
        registered = (
            _systemd_property(detail, "UnitFileState") in {"enabled", "enabled-runtime"}
            or os.name == "nt"
        )
    return dict(mode="automatic", registered=registered, state=state,
                detail=detail or None)


def uninstall(args):
    """Remove updater scheduling while retaining every reachable runtime.

    Selection, process-lease checks and deletion share the exclusive update
    lock. A completed schedule cancellation is reported separately from a
    deferred or failed cleanup. Native plugin removal remains a host action.
    """
    updater = Updater(args.settings)
    with file_lock(updater.root / "checker.lock", exclusive=True), ExitStack() as locks:
        try:
            locks.enter_context(update_lock(updater.config, exclusive=True))
        except BlockingIOError:
            return dict(status="refused", reason="an admitted MindIE call is in flight; retry when idle",
                        removed=[], removed_generations=[], kept_generations=[])
        return _uninstall_locked(args, updater)


def _uninstall_locked(args, updater):
    root = updater.root
    try:
        schedule_disable(updater)
        schedule_note = "schedule removed"
    except Exception as exc:
        # Unknown cancellation cannot authorize any executable/state removal.
        schedule_note = f"schedule removal failed: {type(exc).__name__}: {exc}"
        return dict(status="refused", reason="schedule state is unproven; no updater-owned files were removed",
                    schedule=schedule_note, removed_generations=[], kept_generations=[],
                    retained_caches="preserved (native tasks may still execute them)",
                    recovery_metadata="preserved", errors=[schedule_note])

    retention = updater.collect_generations(exclusive=True)
    removed = [str(root / "generations" / name) for name in retention['removed']]
    kept = [str(root / "generations" / name) for name in [*retention['kept'], *retention['untracked']]]
    if retention['status'] in {'failed', 'deferred'}:
        # Recovery may need any generation, and failed cleanup may already
        # have removed a subset. Preserve the precise separate outcomes.
        return dict(status="partial", schedule=schedule_note, retention=retention,
                    removed_generations=removed, kept_generations=kept or None,
                    remaining_generations="preserved; cleanup did not finish evaluating all references",
                    retained_caches="preserved", recovery_metadata="preserved",
                    errors=[retention.get('error') or retention.get('reason') or 'generation cleanup failed'])

    errors = []
    for extra in ("controller",):
        try:
            shutil.rmtree(root / extra)
        except OSError as exc:
            if (root / extra).exists():
                errors.append(f"cannot remove {extra}: {exc}")
    try:
        (root / "launcher.py").unlink(missing_ok=True)
    except OSError as exc:
        errors.append(f"cannot remove updater launcher: {exc}")
    # Keep the checker inode while this and competing processes can name it.
    # Unlinking a held lock would let another caller lock a replacement inode.
    purged = False
    if args.purge:
        if kept:
            errors.append("refusing --purge: current, candidate, executing, leased or untracked generations remain")
        elif errors:
            errors.append("refusing --purge: earlier updater cleanup failed")
        else:
            try:
                shutil.rmtree(root)
                purged = True
                args.settings.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"updater purge failed: {exc}")
    status = "uninstalled" if not errors else "partial"
    result = dict(status=status, schedule=schedule_note, retention=retention,
                  removed_generations=removed, kept_generations=kept,
                  retained_caches="host-managed caches preserved",
                  recovery_metadata="purged" if purged else "preserved", errors=errors)
    if not purged:
        try:
            atomic(updater.state_path, dict(updater.state, status=status, errors=errors or None, retention=retention))
        except OSError as exc:
            result.update(status="partial", state_persistence="failed")
            result["errors"].append("uninstall outcome persistence failed: " + type(exc).__name__)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".config/mindie-agent/updater.json",
    )
    sub = parser.add_subparsers(dest="operation", required=True)
    start = sub.add_parser("enable")
    start.add_argument("--source-root", type=Path, required=True)
    start.add_argument(
        "--root", type=Path, default=Path.home() / ".local/share/mindie-agent/updates"
    )
    start.add_argument("--channel", choices=["main", "release"], default="main")
    start.add_argument("--schedule", choices=["auto", "manual"], default="auto")
    sub.add_parser("check")
    sub.add_parser("status")
    sub.add_parser("disable")
    remove = sub.add_parser("uninstall")
    remove.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    if args.operation == "enable":
        result = enable(args)
    elif args.operation == "status":
        settings = read(args.settings)
        updater = Updater(args.settings)
        result = dict(
            settings=settings,
            state=read(Path(settings["root"]) / "state.json", {}),
            schedule=schedule_status(updater),
        )
    elif args.operation == "disable":
        updater = Updater(args.settings)
        schedule_disable(updater)
        settings = read(args.settings)
        settings["schedule_mode"] = "manual"
        settings["schedule"] = dict(mode="manual", registered=False)
        atomic(args.settings, settings)
        result = dict(status="disabled")
    elif args.operation == "uninstall":
        result = uninstall(args)
    else:
        result = Updater(args.settings).check()
    print(json.dumps(result, indent=2))
    if (result.get("status") in {"check_failed", "update_failed", "incompatible", "attempts_exhausted", "unavailable", "failed", "refused", "degraded", "action_required", "waiting_for_compatible_source", "partial"}
            or result.get("knowledge_status") in {"sync_failed", "degraded", "failed", "unavailable", "invalid"}
            or result.get("diagnostics", {}).get("status") in {"degraded", "unavailable", "configuration_unavailable", "failed", "error"}):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
