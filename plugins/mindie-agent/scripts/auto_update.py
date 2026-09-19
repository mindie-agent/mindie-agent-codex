#!/usr/bin/env python3
"""Bounded, model-free MindIE updates. Main now; stable GitHub releases later.

Scheduling has a platform boundary: macOS uses a user LaunchAgent, Windows a
scheduled task (implemented, not yet verified on real hardware).
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
DOMAIN_REQUIREMENTS = "domain-requirements.txt"
DOMAIN_SKILLS = (
    "vllm-ascend-serving",
    "vllm-ascend-pd-serving",
    "vllm-ascend-benchmark",
    "vllm-ascend-performance-regression",
    "vllm-ascend-correctness-validation",
    "vllm-ascend-change-validation",
    "vllm-ascend-distributed-debug",
    "vllm-ascend-graph-debug",
    "ascend-operator-debug",
    "ascend-tensor-dump",
    "ascend-memory-profiling",
    "ascend-profiling-collection",
    "ascend-profiling-analysis",
    "modelscope",
)
LABEL = "org.mindie-agent.plugin-updater"
WIN_TASK = "MindIE Agent Plugin Updater"
INTERVAL = 300
TOTAL_TIMEOUT = 240
ATTEMPTS = 3


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


def link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, path)


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
            "retired_entry.py",
            "update_launcher.py",
        ):
            if not (plugin / "scripts" / name).is_file():
                raise Incompatible("missing bounded runtime entry: " + name)
        for skill in DOMAIN_SKILLS:
            if not (plugin / "skills" / skill / "SKILL.md").is_file():
                raise Incompatible("missing domain skill: " + skill)
        requirements = (source / "runtime-requirements.txt").read_text().splitlines()
        pattern = r"([a-z-]+) @ git\+https://github.com/mindie-agent/(knowledge|remote-dev)@([0-9a-f]{40})(#subdirectory=tools/knowledge-intake)?"
        packages = {}
        for line in requirements:
            if not line.strip() or line.startswith("#"):
                continue
            match = re.fullmatch(pattern, line)
            if not match or match[1] in packages:
                raise Incompatible(
                    "runtime dependencies require exact official commit pins"
                )
            packages[match[1]] = (match[2], match[3], match[4])
        if (
            set(packages) != {"mindie-knowledge", "knowledge-intake", "remote-dev"}
            or packages["mindie-knowledge"][0] != "knowledge"
            or packages["knowledge-intake"][0] != "knowledge"
            or packages["mindie-knowledge"][1] != packages["knowledge-intake"][1]
            or packages["knowledge-intake"][2] != "#subdirectory=tools/knowledge-intake"
            or packages["remote-dev"][0] != "remote-dev"
        ):
            raise Incompatible("invalid runtime package combination")
        # Domain execution pins live in a separate file so the previous
        # controller generation (which validates exactly the set above) can
        # still upgrade to this source. This controller requires it.
        domain_lines = (source / DOMAIN_REQUIREMENTS).read_text().splitlines()
        domain_pattern = r"mindie-coordinator @ git\+https://github.com/mindie-agent/coordinator@([0-9a-f]{40})"
        pins = [
            re.fullmatch(domain_pattern, line)
            for line in domain_lines
            if line.strip() and not line.startswith("#")
        ]
        if len(pins) != 1 or not pins[0]:
            raise Incompatible("domain dependencies require an exact coordinator pin")

    def probe_runtime(self, python):
        self.command(
            [
                python,
                "-c",
                "\n".join(
                    [
                        "import inspect, knowledge_intake",
                        "from mindie_knowledge.loop.activation import SessionAdmission",
                        "from mindie_knowledge.loop.budget import MaintenanceBudget as B",
                        "from mindie_knowledge.loop.transport import Service",
                        "from mindie_knowledge.loop.cli import STARTUP_TIMEOUT, MAX_STARTUP_PROBES, TOOLS",
                        "from remote_dev.mcp.tools import call_tool",
                        "from mindie_coordinator.task_client import TaskClient",
                        "from mindie_coordinator.run_manifest import new_manifest",
                        "assert 'session_activation' in inspect.signature(Service).parameters",
                        "assert 0 < B.SESSION_LIMIT <= 6 and 0 < B.HOURLY_LIMIT <= 20 and B.FAILURE_LIMIT <= 3",
                        "assert STARTUP_TIMEOUT <= 5 and MAX_STARTUP_PROBES <= 3",
                        "assert any(t['name'] == 'knowledge_attach' for t in TOOLS)",
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
        )
        self.command(["git", "-C", source, "checkout", "--detach", "-q", "FETCH_HEAD"])
        if self.command(["git", "-C", source, "rev-parse", "HEAD"]).strip() != sha:
            raise ValueError("fetched revision changed")
        self.validate_source(source)
        python = generation / "venv/bin/python"
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
                "-r",
                source / DOMAIN_REQUIREMENTS,
            ],
            timeout=120,
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
        sessions = Sessions(self.config)
        if not sessions.path.exists():
            return 0
        db = sqlite3.connect(sessions.path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        try:
            return db.execute(
                "SELECT count(*) FROM leases WHERE enabled=1 AND expires>? AND failures<3 AND fingerprint=?",
                (time.time(), sessions.fingerprint()),
            ).fetchone()[0]
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

    def preserve_caches(self):
        cache = (
            Path(self.settings["codex_home"])
            / "plugins/cache/mindie-agent/mindie-agent"
        )
        backup = self.root / "retained-caches"
        backup.mkdir(exist_ok=True)
        if cache.exists():
            for version in cache.iterdir():
                if (
                    version.is_dir()
                    and not (version / "scripts/update_lock.py").exists()
                ):
                    # Pre-contract clients cannot coordinate a switch. Retain their
                    # paths as inert shims, and keep the original bytes for audit.
                    original = self.root / "legacy-caches-original" / version.name
                    if not original.exists():
                        shutil.copytree(version, original)
                    for entry in ("bridge.py", "remote_bridge.py"):
                        target = version / "scripts" / entry
                        if target.exists():
                            shutil.copy2(
                                Path(__file__).with_name("retired_entry.py"), target
                            )
                            retained = backup / version.name / "scripts" / entry
                            if retained.exists():
                                shutil.copy2(target, retained)
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
        journal_path.unlink()

    def install(self, candidate):
        with update_lock(self.config, exclusive=True):
            if self.active_sessions():
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
            journal = dict(
                adapter=adapter,
                candidate=candidate["revision"],
                marketplace=existing["root"] if existing else None,
                link=str(plugin_link.resolve()) if plugin_link.exists() else None,
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

    def ensure_domain_runtime(self):
        """Repair a current generation whose venv predates domain-requirements.

        A previous controller generation installs only runtime-requirements.txt;
        the first check run by this controller tops the same pinned venv up with
        the domain dependency. Bounded: at most ATTEMPTS repairs per revision.
        """
        current = self.state.get("current") or {}
        python, revision = current.get("python"), current.get("revision")
        if not python or not revision:
            return
        domain = self.root / "generations" / revision / "source" / DOMAIN_REQUIREMENTS
        if not domain.exists():
            return
        try:
            self.command(
                [python, "-c", "import mindie_coordinator.task_client"], timeout=15
            )
            return
        except Exception:
            pass
        repairs = self.state.setdefault("domain_repairs", {})
        if repairs.get(revision, 0) >= ATTEMPTS:
            return self.save(
                "domain_runtime_incomplete",
                error="coordinator package missing from the current runtime; repair attempts exhausted",
            )
        repairs[revision] = repairs.get(revision, 0) + 1
        self.save("repairing_domain_runtime", candidate=revision)
        try:
            self.command(
                [
                    self.settings["uv"],
                    "pip",
                    "install",
                    "--python",
                    python,
                    "-r",
                    domain,
                ],
                timeout=120,
            )
            self.command(
                [python, "-c", "import mindie_coordinator.task_client"], timeout=15
            )
        except Exception as exc:
            return self.save(
                "domain_runtime_incomplete",
                error=f"domain runtime repair failed: {type(exc).__name__}: {str(exc)[:160]}",
            )

    def _check_locked(self):
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
                repair = self.ensure_domain_runtime()
                if repair is not None:
                    return repair
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
