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
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

from bounded_process import run
from session_gate import Sessions, config_path
from update_lock import file_lock, update_lock

REPOSITORY = "https://github.com/mindie-agent/mindie-agent-codex.git"
CONTRACT = dict(
    schema=1,
    session_admission=1,
    bounded_calls=1,
    idle_update_lock=1,
    maintenance_budget=1,
)
LABEL = "org.mindie-agent.plugin-updater"
WIN_TASK = "MindIE Agent Plugin Updater"
INTERVAL = 300
TOTAL_TIMEOUT = 240
ATTEMPTS = 3
# Knowledge sync is model-free and independently budgeted: it runs inside the
# same 300 s scheduler slot but before any plugin build work, so a slow or
# stuck plugin candidate can never starve it. The knowledge core persists its
# own 30 s/3-attempt per-candidate budget; this is only the outer call bound.
KNOWLEDGE_TIMEOUT = 45


class Incompatible(ValueError):
    pass


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

    def command(self, args, *, timeout=30, data=""):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("update deadline reached")
        # Disable implicit download/transport retries. Each scheduler run gets one attempt.
        env = dict(
            os.environ,
            GIT_TERMINAL_PROMPT="0",
            UV_HTTP_RETRIES="0",
            UV_HTTP_TIMEOUT="20",
            UV_NO_PROGRESS="1",
        )
        return run(
            [str(arg) for arg in args], data, timeout=min(timeout, remaining), env=env
        )

    def save(self, status, **values):
        self.state.update(status=status, checked_at=time.time(), **values)
        atomic(self.state_path, self.state)
        return self.state

    def resolve(self):
        ref = "refs/heads/main"
        if self.settings["channel"] == "release":
            # GitHub's latest endpoint excludes drafts and prereleases.
            request = urllib.request.Request(
                "https://api.github.com/repos/mindie-agent/mindie-agent-codex/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": LABEL},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read(256 * 1024 + 1)
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
            "agent_worker.py",
            "update_idle.py",
            "auto_update.py",
            "update_launcher.py",
        ):
            if not (plugin / "scripts" / name).is_file():
                raise Incompatible("missing bounded runtime entry: " + name)
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
        # The probe must match the actual new package APIs (community sharing
        # contract), never the retired judge/authority surface. A candidate
        # built against old pins fails closed here until the knowledge pin is
        # republished; the root-owned dependency overlay can overlay local
        # core sources for integration testing.
        self.command(
            [
                python,
                "-c",
                "\n".join(
                    [
                        "import inspect",
                        "from mindie_knowledge.loop.cli import TOOLS, STARTUP_TIMEOUT, MAX_STARTUP_PROBES",
                        "from mindie_knowledge.loop.budget import MaintenanceBudget as B",
                        "from mindie_knowledge.loop.transport import Service",
                        "from mindie_knowledge.loop import documents, transcript",
                        "from mindie_knowledge.community import submit_batch, reconcile_batch",
                        "from remote_dev.mcp.tools import call_tool",
                        "names = {t['name'] for t in TOOLS}",
                        "assert {'knowledge_query', 'knowledge_explain', 'knowledge_feedback'} <= names",
                        "assert 'knowledge_use' not in names and 'knowledge_judge' not in names",
                        "assert all(hasattr(documents, n) for n in ('render_entry', 'parse_entry', 'revision_of'))",
                        "assert 'admission' in inspect.signature(Service).parameters",
                        "assert 0 < B.SESSION_LIMIT <= 6 and 0 < B.HOURLY_LIMIT <= 20 and B.FAILURE_LIMIT <= 3",
                        "assert STARTUP_TIMEOUT <= 5 and MAX_STARTUP_PROBES <= 3",
                    ]
                ),
            ],
            timeout=15,
        )

    def apply_dependency_overlay(self, python):
        """Root-only integration mechanism: overlay local core sources.

        Production installs never use this (the key is absent from committed
        settings); exact remote pins stay authoritative. When root records
        local source directories in the updater settings, they are installed
        over the pinned requirements and their exact revision/dirty state is
        captured as integration evidence — never presented as a reproducible
        remote install.
        """
        overlay = self.settings.get("dependency_overlay") or []
        evidence = []
        for entry in overlay:
            source = Path(entry).expanduser().absolute()
            if not (source / "pyproject.toml").is_file():
                raise Incompatible(
                    "dependency overlay entries must be package sources: " + str(source)
                )
            revision = self.command(
                ["git", "-C", source, "rev-parse", "HEAD"], timeout=10
            ).strip()
            dirty = bool(
                self.command(
                    ["git", "-C", source, "status", "--porcelain"], timeout=10
                ).strip()
            )
            self.command(
                [self.settings["uv"], "pip", "install", "--python", python, source],
                timeout=60,
            )
            evidence.append(dict(path=str(source), revision=revision, dirty=dirty))
        return evidence

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
        )
        overlay = self.apply_dependency_overlay(python)
        self.probe_runtime(python)
        result = self.package(generation, source, python, sha)
        if overlay:
            result["dependency_overlay"] = overlay
            atomic(generation / "prepared.json", result)
        return result

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
        mcp = read(plugin / ".mcp.json")
        for server in mcp["mcpServers"].values():
            server["command"] = self.settings["python"]
            server["args"][0] = str(plugin / server["args"][0])
            server["cwd"] = str(plugin)
        atomic(plugin / ".mcp.json", mcp)
        argv = [self.settings["python"], str(plugin / "scripts/bridge.py"), "stop"]
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
            # Pure updater changes leave the reviewed hook executable unchanged.
            # Retain its immutable command instead of changing a path needlessly.
            ignored = {"auto_update.py", "update_launcher.py"}
            files = {
                p.name for p in (plugin / "scripts").iterdir() if p.is_file()
            } - ignored
            old_files = {
                p.name for p in (previous / "scripts").iterdir() if p.is_file()
            } - ignored
            if files == old_files and all(
                (plugin / "scripts" / name).read_bytes()
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

    def active_sessions(self):
        """Active lease count; None when lease state is unreadable.

        The updater only reads leases to decide switch timing. An unreadable
        store defers the switch — it never clears leases or guesses that
        tasks have ended.
        """
        sessions = Sessions(self.config)
        if not sessions.path.exists():
            return 0
        try:
            db = sqlite3.connect(
                sessions.path.as_uri() + "?mode=ro", uri=True, timeout=0.1
            )
        except sqlite3.Error:
            return None
        try:
            return db.execute(
                "SELECT count(*) FROM leases WHERE enabled=1 AND expires>? AND failures<3 AND fingerprint=?",
                (time.time(), sessions.fingerprint()),
            ).fetchone()[0]
        except sqlite3.Error:
            return None
        finally:
            db.close()

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

    def install(self, candidate):
        with update_lock(self.config, exclusive=True):
            active = self.active_sessions()
            if active is None:
                return self.save(
                    "waiting_for_idle",
                    candidate=candidate["revision"],
                    error="lease state unreadable; switch deferred, leases left untouched",
                )
            if active:
                return self.save("waiting_for_idle", candidate=candidate["revision"])
            adapter = read(self.config)
            idle = json.loads(
                self.command(
                    [adapter["python"], Path(__file__).with_name("update_idle.py")],
                    data=json.dumps(adapter),
                    timeout=5,
                )
            )
            if not idle["idle"]:
                return self.save("waiting_for_idle", candidate=candidate["revision"])
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
            try:
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
                engine.update(
                    agent_command=[
                        candidate["python"],
                        str(Path(candidate["plugin"]) / "scripts/agent_worker.py"),
                    ],
                    session_activation=str(self.config),
                )
                engine_path = Path(candidate["plugin"]).parent / "engine.json"
                atomic(engine_path, engine)
                atomic(
                    self.config,
                    dict(
                        adapter,
                        python=candidate["python"],
                        engine_config=str(engine_path),
                    ),
                )
                result = self.save(
                    "installed",
                    error=None,
                    current=candidate,
                    candidate=candidate["revision"],
                    activation="new task required; changed hooks require native trust review",
                )
                (self.root / "transaction.json").unlink()
                return result
            except Exception:
                self.recover()
                raise
            finally:
                self.restore_caches()

    def check(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with file_lock(self.root / "checker.lock", exclusive=True):
                self.state = read(self.state_path, {})
                return self._check_locked()
        except BlockingIOError:
            return dict(status="already_running")

    def check_knowledge(self):
        """Model-free knowledge sync on the same 300 s schedule.

        Independent of plugin build state and of the community sharing switch:
        sharing off still receives published updates. One bounded call into the
        knowledge core, which owns its own per-candidate attempt persistence;
        failures are recorded under knowledge_* keys and never mask or cancel
        the plugin check that follows.
        """
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
        # Actual core surface: `sync` prints one JSON list of per-feed results.
        result = json.loads(output) if output.strip() else []
        if not isinstance(result, list):
            raise ValueError("unexpected knowledge sync result shape")
        self.save(
            self.state.get("status", "unknown"),
            knowledge_status="ok",
            knowledge_feeds=len(result),
            knowledge_error=None,
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
                    knowledge_checked_at=time.time(),
                )
            try:
                return self._check_plugin()
            except Exception as exc:
                # The plugin check owns its failure states; this guard only
                # keeps an unexpected crash from hiding the knowledge result.
                return self.save(
                    "update_failed",
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                )

    def _check_plugin(self):
            if self.state.get("next_check", 0) > time.time():
                return self.state
            try:
                if (self.root / "transaction.json").exists():
                    with update_lock(self.config, exclusive=True):
                        self.recover()
                sha = self.resolve()
            except Exception as exc:
                failures = self.state.get("check_failures", 0) + 1
                return self.save(
                    "check_failed",
                    check_failures=failures,
                    error=str(exc)[:240],
                    next_check=time.time()
                    + (3600 if failures >= ATTEMPTS else INTERVAL),
                )
            self.state.update(
                check_failures=0, next_check=time.time() + INTERVAL, error=None
            )
            if self.state.get("current", {}).get("revision") == sha:
                return self.save("up_to_date")
            attempts = self.state.setdefault("attempts", {})
            record = attempts.setdefault(sha, dict(count=0))
            if record.get("incompatible") or record["count"] >= ATTEMPTS:
                return self.save(
                    "waiting_for_compatible_source"
                    if record.get("incompatible")
                    else "attempts_exhausted",
                    candidate=sha,
                    error=record.get(
                        "reason", "source lacks required compatibility contract"
                    )
                    if record.get("incompatible")
                    else record.get("last_error", "update attempt limit reached"),
                )
            # A crash consumes an attempt. A normal idle deferral refunds it.
            record["count"] += 1
            self.save("preparing", candidate=sha)  # Reserve before doing fallible work.
            try:
                candidate = self.prepare(sha)
                result = self.install(candidate)
                if result["status"] == "waiting_for_idle":
                    record["count"] -= 1
                    return self.save("waiting_for_idle", candidate=sha)
                return result
            except BlockingIOError:
                record["count"] -= 1
                return self.save("waiting_for_idle", candidate=sha)
            except Incompatible as exc:
                record["incompatible"] = True
                record["reason"] = str(exc)
                return self.save(
                    "waiting_for_compatible_source", error=str(exc), candidate=sha
                )
            except Exception as exc:
                record["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                return self.save(
                    "update_failed",
                    error=record["last_error"],
                    candidate=sha,
                )


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


def schedule_disable(updater):
    """Remove the platform schedule; installed plugin and state stay in place."""
    if sys.platform == "darwin":
        updater.command(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], timeout=10
        )
        (Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")).unlink(
            missing_ok=True
        )
    elif os.name == "nt":
        # Windows (unverified on real hardware).
        updater.command(["schtasks", "/Delete", "/TN", WIN_TASK, "/F"], timeout=15)
    else:
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
        if result["status"] != "installed":
            raise RuntimeError(
                "deactivate active MindIE sessions before enabling updater"
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

    Order: preflight every active reference first (leases, live generation,
    interrupted transaction); a refusal deletes nothing. Scheduling removal is
    reported separately from state removal. Retained native caches are never
    deleted here: loaded tasks may still execute those trusted entrypoints.
    Rollback/recovery metadata (state.json, transaction.json) is preserved
    unless --purge runs with no live references.
    """
    updater = Updater(args.settings)
    root = updater.root
    with file_lock(root / "checker.lock", exclusive=True):
        # Preflight: refuse while any valid manual lease exists.
        sessions = Sessions(updater.config)
        active = 0
        if sessions.path.exists():
            db = sqlite3.connect(
                sessions.path.as_uri() + "?mode=ro", uri=True, timeout=0.1
            )
            try:
                active = db.execute(
                    "SELECT count(*) FROM leases WHERE enabled=1 AND expires>?",
                    (time.time(),),
                ).fetchone()[0]
            finally:
                db.close()
        if active:
            return dict(
                status="refused",
                reason=f"{active} active MindIE session lease(s); deactivate them first",
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
            schedule_note = f"schedule removal failed: {type(exc).__name__}: {exc}"
            errors.append(schedule_note)

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
