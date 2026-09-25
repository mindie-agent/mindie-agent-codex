#!/usr/bin/env python3
"""Bounded, model-free MindIE updates. Main now; stable GitHub releases later.

Scheduling has a platform boundary: macOS uses a user LaunchAgent, Windows a
scheduled task (implemented, not yet verified on real hardware). Filesystem
publishing uses an atomic symlink swap on POSIX; on Windows it uses an
unprivileged directory junction with a non-atomic swap covered by the
transaction journal (likewise unverified on real hardware).
"""

import argparse
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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from bounded_process import classify_transport_text, run
from session_gate import config_path, runtime_scripts
from update_lock import file_lock, update_lock

REPOSITORY = "https://github.com/mindie-agent/mindie-agent-codex.git"
CONTRACT = dict(
    schema=1,
    session_admission=1,
    bounded_calls=1,
    idle_update_lock=1,
    maintenance_budget=1,
    admission_path=1,
    transcript_adapter=1,
)
LABEL = "org.mindie-agent.plugin-updater"
WIN_TASK = "MindIE Agent Plugin Updater"
INTERVAL = 300
TOTAL_TIMEOUT = 240
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
# Knowledge sync is model-free and independently budgeted: it runs inside the
# same 300 s scheduler slot but before any plugin build work, so a slow or
# stuck plugin candidate can never starve it. The knowledge core persists its
# own 30 s/3-attempt per-candidate budget; this is only the outer call bound.
KNOWLEDGE_TIMEOUT = 45


class Incompatible(ValueError):
    pass


_FEED_OK = frozenset({"synced", "unchanged"})
_FEED_PENDING = frozenset({"busy", "deferred"})
_FEED_ERROR = frozenset({"unavailable", "invalid", "exhausted"})
_FEED_KNOWN = _FEED_OK | _FEED_PENDING | _FEED_ERROR


def fold_feed_results(output):
    """Fold core `sync` stdout, which MUST be one JSON list (no prefix/suffix).

    Empty list is ok. busy/deferred means not completed now, not a failed
    attempt: all-pending folds to deferred. Mix of completed (synced/unchanged)
    with pending or failed folds to degraded. No completed rows plus at least
    one unavailable/invalid/exhausted folds to sync_failed. Every original
    row is preserved. Unknown status or malformed stdout raises.
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
    failed = sum(1 for row in rows if row["status"] in _FEED_ERROR)
    if not rows or good == len(rows):
        aggregate = "ok"
        summary = None
    elif good and (pending or failed):
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
        if row["status"] in _FEED_OK:
            continue
        detail = row.get("detail") or row.get("cause") or ""
        parts.append(f"{row['repository']}:{row['status']}:{str(detail)[:80]}")
    return "; ".join(parts)[:240]


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
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
        subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(temporary), str(target)],
            check=True,
            capture_output=True,
            timeout=10,
        )
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


class Updater:
    def __init__(self, settings):
        self.settings_path = Path(settings).absolute()
        self.settings = read(settings)
        self.root = Path(self.settings["root"])
        self.config = Path(self.settings["adapter_config"])
        self.state_path = self.root / "state.json"
        self.state = read(self.state_path, {})
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.command_deadline = self.deadline

    def command(self, args, *, timeout=30, data="", allowed_returncodes=(0,), transport=False):
        remaining = min(self.deadline, self.command_deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("update deadline reached")
        # Disable implicit download/transport retries. Each scheduler run gets one attempt.
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "PYTHONPATH"
        }
        env.update(
            GIT_TERMINAL_PROMPT="0",
            UV_HTTP_RETRIES="0",
            UV_HTTP_TIMEOUT="20",
            UV_NO_PROGRESS="1",
            MINDIE_AGENT_CONFIG=str(self.config),
            CODEX_HOME=self.settings["codex_home"],
            MINDIE_CODEX_BIN=self.settings["codex"],
        )
        return run(
            [str(arg) for arg in args], data, timeout=min(timeout, remaining), env=env,
            allowed_returncodes=allowed_returncodes, transport=transport,
        )

    def save(self, status, **values):
        self.state.update(status=status, checked_at=time.time(), **values)
        if (self.state.get("service_handoff") or {}).get("status") in {"failed", "pending"}:
            if status in {"installed", "up_to_date"}:
                self.state["status"] = "degraded"
        atomic(self.state_path, self.state)
        return self.state

    def _read_release(self, request):
        """Read release JSON. Failure text is classified and then discarded."""
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
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
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", tag):
                raise ValueError("invalid release tag")
            ref = "refs/tags/" + tag
        elif self.settings["channel"] != "main":
            raise ValueError("unsupported update channel")
        output = self.command(
            ["git", "ls-remote", self.settings["repository"], ref, ref + "^{}"],
            timeout=15,
            transport=True,
        )
        refs = dict(line.split()[::-1] for line in output.splitlines())
        sha = refs.get(ref + "^{}", refs.get(ref, ""))
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("update source did not resolve to a commit")
        return sha

    def validate_source(self, source):
        if read(source / "update-contract.json", {}) != CONTRACT:
            raise Incompatible(
                "main lacks the manual-session, timeout and update-lock contract"
            )
        plugin = source / "plugins/mindie-agent"
        if any(path.is_symlink() for path in plugin.rglob("*")):
            raise Incompatible("plugin source must contain regular files")
        if (
            sum(p.stat().st_size for p in plugin.rglob("*") if p.is_file())
            > 16 * 1024 * 1024
        ):
            raise Incompatible("plugin package exceeds 16 MiB")
        if (
            "allow_implicit_invocation: false"
            not in (plugin / "skills/mindie-agent/agents/openai.yaml").read_text()
        ):
            raise Incompatible("implicit invocation is enabled")
        hooks = read(plugin / "hooks/hooks.json")["hooks"]
        if set(hooks) != {"Stop"} or len(hooks["Stop"]) != 1:
            raise Incompatible("only one bounded Stop hook is supported")
        entries = hooks["Stop"][0]["hooks"]
        if len(entries) != 1 or not 0 < entries[0]["timeout"] <= 2:
            raise Incompatible("invalid hook deadline")
        for name in (
            "session_gate.py",
            "mcp_gate.py",
            "update_lock.py",
            "bounded_process.py",
            "runtime_call.py",
            "admission_ops.py",
            "codex_transcript.py",
            "agent_worker.py",
            "service_handoff.py",
            "auto_update.py",
            "update_launcher.py",
            "mcp_catalog.json",
        ):
            if not (plugin / "scripts" / name).is_file():
                raise Incompatible("missing bounded runtime entry: " + name)
        catalog = read(plugin / "scripts" / "mcp_catalog.json", {})
        names = {tool.get("name") for tool in catalog.get("knowledge") or []}
        if not {"knowledge_query", "knowledge_explain", "knowledge_feedback"} <= names:
            raise Incompatible("adapter knowledge catalogue is incomplete")
        if "knowledge_use" in names or "knowledge_judge" in names:
            raise Incompatible("retired knowledge tools are advertised")
        requirements = (source / "runtime-requirements.txt").read_text().splitlines()
        pattern = r"([a-z-]+) @ git\+https://github.com/mindie-agent/(knowledge|remote-dev)@([0-9a-f]{40})"
        packages = {}
        for line in requirements:
            if not line.strip() or line.startswith("#"):
                continue
            match = re.fullmatch(pattern, line)
            if not match or match[1] in packages:
                raise Incompatible(
                    "runtime dependencies require exact official commit pins"
                )
            packages[match[1]] = (match[2], match[3])
        if (
            set(packages) != {"mindie-knowledge", "remote-dev"}
            or packages["mindie-knowledge"][0] != "knowledge"
            or packages["remote-dev"][0] != "remote-dev"
        ):
            raise Incompatible("invalid runtime package combination")

    def probe_runtime(self, python):
        # The probe must match the actual new package APIs (persistent core
        # admission, adapter-owned transcript parser, stop_if_idle). Knowledge
        # tools live in the adapter catalogue, not a retired core TOOLS list.
        # Production installs only the exact official remote pins.
        self.command(
            [
                python,
                "-c",
                "\n".join(
                    [
                        "import inspect, math",
                        "from mindie_knowledge.loop.cli import STARTUP_TIMEOUT, MAX_STARTUP_PROBES, load_transcript_adapter",
                        "from mindie_knowledge.loop.activation import Admission",
                        "from mindie_knowledge.loop.budget import MaintenanceBudget as B",
                        "from mindie_knowledge.loop.engine import Engine",
                        "from mindie_knowledge.loop.transport import Service",
                        "from mindie_knowledge.loop import documents",
                        "from mindie_knowledge.community import submit_batch, reconcile_batch",
                        "from remote_dev.mcp.tools import call_tool",
                        "assert callable(load_transcript_adapter)",
                        "assert callable(call_tool)",
                        "assert callable(Engine.stop_if_idle)",
                        "assert callable(getattr(Service, '_stop_if_idle', None))",
                        "assert all(hasattr(documents, n) for n in ('render_entry', 'parse_entry', 'revision_of'))",
                        "assert 'path' in inspect.signature(Admission.__init__).parameters",
                        "assert all(hasattr(Admission, n) for n in ('activate', 'check', 'resolve', 'claim', 'finish', 'deactivate', 'capture_lease', 'active_lease', 'scope_root', 'allows_hash', 'leases'))",
                        "assert 'admission' in inspect.signature(Service).parameters",
                        "assert all(type(getattr(B, n)) is int and getattr(B, n) > 0 for n in ('SESSION_LIMIT', 'HOURLY_LIMIT', 'FAILURE_LIMIT'))",
                        "assert type(B.SESSION_WINDOW) in (int, float) and math.isfinite(B.SESSION_WINDOW) and B.SESSION_WINDOW > 0",
                        "assert type(STARTUP_TIMEOUT) in (int, float) and math.isfinite(STARTUP_TIMEOUT) and STARTUP_TIMEOUT > 0",
                        "assert type(MAX_STARTUP_PROBES) is int and MAX_STARTUP_PROBES > 0",
                    ]
                ),
            ],
            timeout=15,
        )

    def prepare(self, sha):
        generation = self.root / "generations" / sha
        receipt = generation / "prepared.json"
        if receipt.exists():
            return read(receipt)
        if generation.exists():
            shutil.rmtree(generation)  # Only our uncommitted, incomplete staging area.
        source = generation / "source"
        source.mkdir(parents=True)
        self.command(["git", "init", "-q", source])
        self.command(
            [
                "git",
                "-C",
                source,
                "fetch",
                "--depth=1",
                self.settings["repository"],
                sha,
            ],
            timeout=45,
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
            timeout=15,
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
            timeout=120,
            transport=True,
        )
        self.probe_runtime(python)
        return self.package(generation, source, python, sha)

    def package(self, generation, source, python, revision):
        plugin = generation / "plugin"
        shutil.copytree(
            source / "plugins/mindie-agent",
            plugin,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
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
        # A loaded task keeps an immutable entrypoint even if Codex removes its cache.
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
        for server in mcp["mcpServers"].values():
            server["command"] = self.settings["python"]
            server["args"][0] = str(plugin / server["args"][0])
            server["cwd"] = str(plugin)
            env = dict(server.get("env") or {})
            env["MINDIE_AGENT_CONFIG"] = config_value
            server["env"] = env
            server.pop("env_vars", None)
        atomic(plugin / ".mcp.json", mcp)
        argv = [
            self.settings["python"],
            str(plugin / "scripts/bridge.py"),
            "--config",
            config_value,
            "stop",
        ]
        if os.name == "nt":
            # Explicit cmd boundary works even if the host uses PowerShell.
            # echo runs after missing executables/paths and masks hook failures.
            inner = subprocess.list2cmdline(argv) + " >NUL 2>&1 & echo {}"
            command = 'cmd.exe /d /s /c "' + inner + '"'
        else:
            command = shlex.join(argv) + " >/dev/null 2>&1; printf '{}\\n'"
        atomic(
            plugin / "hooks/hooks.json",
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": command, "timeout": 2}
                            ]
                        }
                    ]
                }
            },
        )
        previous = self.state.get("current", {}).get("plugin")
        if previous:
            previous = Path(previous)
            # Preserve the immutable reviewed Stop executable only when its
            # complete local execution dependency set is byte-identical.
            # Stop imports these helpers, including diagnostic_support via
            # bridge._record_stop and diagnostic_fallback via its _record.
            # diagnostic-build.json is metadata and does not change that
            # logic, so it must not invalidate an identical Stop command.
            # A changed executable dependency still requires native review.
            files = {
                "bridge.py", "bounded_process.py", "session_gate.py",
                "sharing.py", "update_lock.py", "installation.json",
                "diagnostic_support.py", "diagnostic_fallback.py",
            }
            if all(
                (previous / "scripts" / name).is_file()
                and (plugin / "scripts" / name).read_bytes()
                == (previous / "scripts" / name).read_bytes()
                for name in files
            ):
                shutil.copy2(previous / "hooks/hooks.json", plugin / "hooks/hooks.json")
        result = dict(
            revision=revision,
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
                    timeout=20,
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

    def verify_native(self, version):
        """Fail unless native inventory resolves exactly this installed version."""
        entry = self.native_plugin_entry()
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

    def preserve_caches(self):
        # Loaded native tasks may still execute cached entrypoints: retain
        # their exact bytes untouched. There is no compatibility shim for
        # pre-contract caches — they are kept inert, never rewritten.
        cache = (
            Path(self.settings["codex_home"])
            / "plugins/cache/mindie-agent/mindie-agent"
        )
        backup = self.root / "retained-caches"
        backup.mkdir(exist_ok=True)
        if cache.exists():
            for version in cache.iterdir():
                if version.is_dir() and not (backup / version.name).exists():
                    shutil.copytree(version, backup / version.name)

    def restore_caches(self):
        cache = (
            Path(self.settings["codex_home"])
            / "plugins/cache/mindie-agent/mindie-agent"
        )
        cache.mkdir(parents=True, exist_ok=True)
        for version in (self.root / "retained-caches").glob("*"):
            if not (cache / version.name).exists():
                shutil.copytree(version, cache / version.name)

    def recover(self):
        journal_path = self.root / "transaction.json"
        if not journal_path.exists():
            return
        journal = read(journal_path)
        if self.state.get("current", {}).get("revision") == journal.get("candidate"):
            self.restore_caches()
            self.verify_native(self.state["current"]["version"])
            journal_path.unlink()
            return
        if journal.get("recoveries", 0) >= ATTEMPTS:
            raise RuntimeError(
                "rollback needs operator attention; automatic recovery exhausted"
            )
        journal["recoveries"] = journal.get("recoveries", 0) + 1
        atomic(journal_path, journal)
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
        self.restore_caches()
        # Retained caches must not pollute the rollback either: the native
        # selection after recovery has to be the version the journal restored.
        if journal.get("marketplace") and journal.get("previous_version"):
            self.verify_native(journal["previous_version"])
        journal_path.unlink()

    def restore_service(self, final_proven):
        """No lock-recursive launcher; one restore with the actual adapter tuple."""
        try:
            if not final_proven:
                raise RuntimeError("native recovery is unproven")
            selected = read(self.config)
            output = self.command(
                [selected["python"], Path(__file__).with_name("service_handoff.py"),
                 "restore", selected["engine_config"]], timeout=8)
            result = json.loads(output)
            if result.get("status") not in {"restored", "not-needed"}:
                raise RuntimeError("invalid restoration result")
        except Exception as exc:
            result = dict(status="failed", error=type(exc).__name__)
        self.save(self.state.get("status", "update_failed"), service_handoff=result)

    def install(self, candidate):
        # Actual-idle switching: the exclusive operation lock waits for any
        # in-flight admitted call (holders of the shared lock), and the idle
        # probe waits for maintenance and in-flight publication. Idle task
        # authorizations — enabled leases with no running work — never
        # block an update; unreadable admission state fails their calls
        # closed but is not an update concern either.
        with update_lock(self.config, exclusive=True):
            adapter = read(self.config)
            idle_helper = Path(__file__).with_name("service_handoff.py")
            if not idle_helper.is_file():
                raise Incompatible("updater is missing service_handoff.py")
            if self.deadline - time.monotonic() < 43:
                raise TimeoutError("insufficient time for stop, rollback and restore")
            previous_handoff = self.state.get("service_handoff")
            self.save(self.state.get("status", "preparing"), service_handoff=dict(
                status="pending", error="interrupted-stop-or-restore-needs-attention"))
            try:
                idle = json.loads(
                    self.command(
                        [adapter["python"], idle_helper, "stop", adapter["engine_config"]],
                        timeout=5,
                    )
                )
            except Exception:
                self.save("update_failed", service_handoff=dict(
                    status="failed", error="stop-outcome-unconfirmed"))
                raise RuntimeError("stop outcome unconfirmed; service needs attention") from None
            if idle.get("service") != "stopped":
                self.save(self.state.get("status", "preparing"),
                          service_handoff=previous_handoff)
            if not idle["idle"]:
                return self.save("waiting_for_idle", candidate=candidate["revision"])
            stopped = idle.get("service") == "stopped"
            installed = False
            final_proven = True  # prior native state, before any install mutation
            # Leave a bounded rollback + restoration tail inside this check.
            self.command_deadline = self.deadline - 38
            try:
                existing = self.marketplace()
                if (
                    existing
                    and existing.get("marketplaceSource", {}).get("sourceType") != "local"
                ):
                    raise Incompatible(
                        "only the existing local MindIE marketplace can be migrated"
                    )
                market = self.root / "marketplace"
                plugin_link = market / "plugins/mindie-agent"
                self.preserve_caches()
                previous_native = self.native_plugin_entry()
                journal = dict(
                    adapter=adapter,
                    candidate=candidate["revision"],
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
                self.restore_caches()
                # The add receipt is not proof: retained caches compete in
                # native discovery. Verify the actual resolved version or roll
                # back instead of reporting a fake installed state.
                self.verify_native(candidate["version"])
                engine = read(adapter["engine_config"])
                # One committed generation: worker, transcript parser and
                # interpreter move together; the neutral admission store and
                # any legacy activation key are adapter-scope, not per
                # generation. The old session_activation alias is removed
                # instead of kept as a second name.
                engine.pop("session_activation", None)
                engine.update(
                    agent_command=[
                        candidate["python"],
                        str(Path(candidate["plugin"]) / "scripts/agent_worker.py"),
                    ],
                    transcript_adapter=str(
                        Path(candidate["plugin"]) / "scripts/codex_transcript.py"
                    ),
                )
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
                    ),
                )
                final_proven = True
                result = self.save(
                    "installed",
                    error=None,
                    current=candidate,
                    candidate=candidate["revision"],
                    activation="task authorization preserved; refreshed host definitions load in new tasks; changed hooks require native trust review",
                )
                (self.root / "transaction.json").unlink()
                installed = True
                try:
                    self.publish_stable_launcher()
                except Exception as exc:
                    self.state["launcher_error"] = type(exc).__name__
                return result
            except Exception:
                final_proven = False
                self.command_deadline = self.deadline - 8
                self.recover()
                final_proven = True
                try:
                    self.publish_stable_launcher()
                except Exception:
                    pass
                raise
            finally:
                self.command_deadline = self.deadline
                if stopped:
                    self.restore_service(final_proven)
                if installed:
                    self.save("installed")
                self.restore_caches()

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
            # Pass the REAL remaining window: the CLI's 75s default includes
            # offline work and skips the upgrade unless a full 60s handoff
            # plus 1s exit remains; 2s is this parent's exit/startup margin.
            available = min(75, self.deadline - time.monotonic(),
                            self.command_deadline - time.monotonic())
            if available <= 0:
                return {"status": "deferred", "error_type": "insufficient_budget"}
            output = self.command(
                [python, "-m", "mindie_diagnostics.cli", "reporting", "maintain",
                 "--update-running", "--budget-seconds", str(max(0, available - 2))],
                timeout=available,
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
        try:
            with file_lock(self.root / "checker.lock", exclusive=True):
                self.state = read(self.state_path, {})
                result = self._check_locked()
        except BlockingIOError:
            return dict(status="already_running")
        maintenance = self.maintain_diagnostics()
        try:
            atomic(self.root / "diagnostics-maintenance.json", maintenance)
        except Exception:
            pass
        return dict(result, diagnostics=maintenance)

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
                timeout=KNOWLEDGE_TIMEOUT,
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


def _native_run(updater, argv, timeout):
    """One bounded native scheduler control command with a KNOWN return code.

    bounded_process.run returns stdout only, but scheduler truth needs the
    returncode (launchctl print exit 113 is positive missing-service
    evidence; anything else is not absence) and a capped stderr diagnostic.
    These are fixed small-output OS control commands (launchctl/PowerShell),
    so a plain bounded subprocess.run suffices. Bounded by the updater's
    absolute deadline. Returns (returncode, stdout, stderr); raises
    TimeoutError when the updater budget is spent, subprocess.TimeoutExpired
    past the bounded deadline, and OSError when the manager executable
    itself is unavailable.
    """
    remaining = min(updater.deadline, updater.command_deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("update deadline reached")
    completed = subprocess.run(
        [str(arg) for arg in argv], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, errors="replace",
        timeout=min(timeout, remaining))
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _launchd_state(updater, label, timeout=5):
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


def _task_state(updater, task, timeout=20):
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


def schedule_enable(updater, launcher, settings_path):
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
                ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], timeout=5
            )
        except RuntimeError:
            pass
        else:
            updater.command(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], timeout=10
            )
        updater.command(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", plist], timeout=10
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
            timeout=15,
        )
        return WIN_TASK
    raise ValueError("automatic scheduling requires macOS launchd or Windows schtasks")


def schedule_disable(updater, *, label=None, plist_path=None, schedule_root=None,
                     task=None):
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
    raise ValueError("automatic scheduling requires macOS launchd or Windows schtasks")


def enable(args):
    source = args.source_root.expanduser().absolute()
    root = args.root.expanduser().absolute()
    settings_path = args.settings.expanduser().absolute()
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
    if not settings["codex"] or not settings["uv"]:
        raise ValueError("codex and uv are required")
    root.mkdir(parents=True, exist_ok=True)
    atomic(settings_path, settings)
    updater = Updater(settings_path)
    if (root / "transaction.json").exists():
        with update_lock(updater.config, exclusive=True):
            updater.recover()
    candidate = updater.state.get("current")
    if not candidate:
        updater.validate_source(source)
        updater.probe_runtime(read(updater.config)["python"])
        # Preserve local safety fixes without altering a dirty checkout or inventing a remote revision.
        generation = (
            root
            / "generations"
            / ("local-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
        )
        generation.mkdir(parents=True)
        candidate = updater.package(
            generation, source, read(updater.config)["python"], generation.name
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
    registration = schedule_enable(updater, launcher, settings_path)
    return dict(
        status="enabled",
        channel=args.channel,
        settings=str(settings_path),
        schedule=registration,
    )


def uninstall(args):
    """Remove updater scheduling and updater-owned state, never the live plugin.

    Order: preflight every active reference first (in-flight calls, live
    generation, interrupted transaction); a refusal deletes nothing. Idle
    task authorizations do not block uninstall: the neutral admission store
    is adapter-owned state outside the updater root and is left untouched.
    Scheduling removal is reported separately from state removal. Retained
    native caches are never deleted here: loaded tasks may still execute
    those trusted entrypoints. Rollback/recovery metadata (state.json,
    transaction.json) is preserved unless --purge runs with no live
    references.
    """
    updater = Updater(args.settings)
    root = updater.root
    with file_lock(root / "checker.lock", exclusive=True):
        # Preflight: refuse only while an actual call holds the operation
        # lock; idle authorizations are not active references.
        try:
            with update_lock(updater.config, exclusive=True):
                pass
        except (BlockingIOError, OSError):
            return dict(
                status="refused",
                reason="an admitted MindIE call is in flight; retry when idle",
                removed=[],
            )
        current = updater.state.get("current", {})
        live = (
            Path(current["plugin"]).resolve() if current.get("plugin") else None
        )
        interrupted = (root / "transaction.json").exists()

        errors = []
        try:
            schedule_disable(updater)
            schedule_note = "schedule removed"
        except Exception as exc:
            # The schedule state is unproven: refuse BEFORE any executable
            # or material removal. Generations, controller, launcher,
            # config, and recovery metadata stay exactly as they were; no
            # best-effort continued destruction. The shared reporter is
            # never touched in any uninstall path.
            schedule_note = f"schedule removal failed: {type(exc).__name__}: {exc}"
            return dict(
                status="refused",
                reason="schedule state is unproven; no updater-owned files "
                       "were removed",
                schedule=schedule_note,
                removed_generations=[],
                kept_generations=[],
                retained_caches="preserved (native tasks may still execute them)",
                recovery_metadata="preserved",
                errors=[schedule_note],
            )

        removed, kept = [], []
        generations = root / "generations"
        if generations.exists():
            for path in sorted(generations.glob("*")):
                target = (path / "plugin").resolve()
                if live is not None and target == live:
                    kept.append(str(path))
                    continue
                try:
                    shutil.rmtree(path)
                    removed.append(str(path))
                except OSError as exc:
                    errors.append(f"cannot remove {path}: {exc}")
                    kept.append(str(path))
        for extra in ("controller",):
            try:
                shutil.rmtree(root / extra)
            except OSError as exc:
                if (root / extra).exists():
                    errors.append(f"cannot remove {extra}: {exc}")
        for extra in ("launcher.py", "checker.lock"):
            (root / extra).unlink(missing_ok=True)
        purged = False
        if args.purge:
            if live is not None:
                errors.append(
                    "refusing --purge: the installed plugin still points at "
                    + str(live)
                    + "; uninstall the Codex plugin first"
                )
            elif interrupted:
                errors.append(
                    "refusing --purge: transaction.json holds recovery metadata "
                    "for an interrupted install; run a check to recover first"
                )
            else:
                shutil.rmtree(root)
                purged = True
                args.settings.unlink(missing_ok=True)
        status = "uninstalled" if not errors else "partial"
        result = dict(
            status=status,
            schedule=schedule_note,
            removed_generations=removed,
            kept_generations=kept,
            retained_caches="preserved (native tasks may still execute them)",
            recovery_metadata="purged" if purged else "preserved",
            errors=errors,
        )
        if not purged:
            atomic(updater.state_path, dict(updater.state, status=status, errors=errors or None))
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
        result = dict(
            settings=settings, state=read(Path(settings["root"]) / "state.json", {})
        )
    elif args.operation == "disable":
        schedule_disable(Updater(args.settings))
        result = dict(status="disabled")
    elif args.operation == "uninstall":
        result = uninstall(args)
    else:
        result = Updater(args.settings).check()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
