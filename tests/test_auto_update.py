"""Exercise two real Git revisions without network, models or a real Codex install."""

import json
import io
import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests.process_fixtures import cleanup_temporary_directory

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import auto_update
import product_contract
import candidate_validate
from auto_update import Updater, atomic, read
from session_gate import Sessions, bind_explicit_config
from update_lock import update_lock


class LocalUpdater(Updater):
    installs = 0
    builds = 0
    fail_install = False
    idle = True
    knowledge_calls = 0
    fail_knowledge = False

    def command(self, args, **kwargs):
        args = [str(arg) for arg in args]
        if args[0] == "fixture-codex":
            marketplace = self.root / "fixture-marketplace.json"
            if args[1:4] == ["plugin", "marketplace", "list"]:
                return json.dumps(
                    dict(
                        marketplaces=[read(marketplace)] if marketplace.exists() else []
                    )
                )
            if args[1:4] == ["plugin", "marketplace", "remove"]:
                marketplace.unlink()
            if args[1:4] == ["plugin", "marketplace", "add"]:
                atomic(
                    marketplace,
                    dict(
                        name="mindie-agent",
                        root=args[4],
                        marketplaceSource=dict(sourceType="local"),
                    ),
                )
            elif args[1:3] == ["plugin", "add"]:
                self.installs += 1
                # Simulate the proven native behavior (codex-cli 0.153.4,
                # isolated CODEX_HOME): add prunes all other version
                # directories and caches the marketplace plugin tree
                # byte-identically under its manifest version.
                cache = self.native_cache()
                for entry in cache.iterdir():
                    if entry.is_dir():
                        shutil.rmtree(entry)
                marketplace = read(self.root / "fixture-marketplace.json")
                plugin_source = (
                    Path(marketplace["root"]) / "plugins/mindie-agent"
                )
                manifest = read(
                    plugin_source / ".codex-plugin/plugin.json"
                )
                shutil.copytree(
                    plugin_source,
                    cache / manifest["version"],
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                if self.fail_install:
                    self.fail_install = False  # Rollback CLI succeeds.
                    raise RuntimeError("fixture installation failed")
            elif args[1:3] == ["plugin", "list"]:
                return json.dumps(self.native_list())
            return "{}"
        if args[0] == "fixture-uv":
            if args[1] == "venv":
                runtime = Path(args[-1]) / (
                    "Scripts/python.exe" if os.name == "nt" else "bin/python"
                )
                runtime.parent.mkdir(parents=True)
                runtime.touch()
                self.builds += 1
            return ""
        if len(args) > 1 and args[1].endswith("service_handoff.py"):
            return json.dumps(dict(idle=self.idle))
        return super().command(args, **kwargs)

    def prepare_capture(self, candidate):
        return dict(capture_mode="public-transcript", redactor_executable=str(Path(candidate["python"]).absolute()),
                    summary_command=[candidate["python"], str(Path(candidate["plugin"]) / "scripts/agent_worker.py")])

    def probe_runtime(self, python, scripts=None, *, revision=None, verified_receipt=None):
        self.assert_runtime = Path(python).exists()
        if not self.assert_runtime:
            raise RuntimeError("runtime missing")
        source = product_contract.source_root(scripts or SCRIPTS)
        candidate_validate.adapter_check(source)
        return dict(product_contract.identity(source, revision), status="validated")

    def native_cache(self):
        return (
            Path(self.settings["codex_home"])
            / "plugins/cache/mindie-agent/mindie-agent"
        )

    @staticmethod
    def native_key(name):
        # Proven codex-cli 0.153.4 rule: highest base version, then the
        # numerically greatest build metadata, over cache directory NAMES.
        base, _, build = name.partition("+")
        try:
            base_key = tuple(int(part) for part in base.split("."))
        except ValueError:
            base_key = (0,)
        stamp = build.removeprefix("codex.")
        return (base_key, int(stamp) if stamp.isdigit() else 0, name)

    def native_list(self):
        """Simulated list-time discovery over the cache directory names."""
        cache = self.native_cache()
        versions = [
            p.name for p in cache.iterdir()
            if p.is_dir() and p.name.partition("+")[0].replace(".", "").isdigit()
        ] if cache.exists() else []
        installed = []
        if versions:
            selected = max(versions, key=self.native_key)
            installed.append(
                dict(
                    pluginId="mindie-agent@mindie-agent",
                    name="mindie-agent",
                    marketplaceName="mindie-agent",
                    version=selected,
                    installed=True,
                    enabled=True,
                )
            )
        return dict(installed=installed, available=[])

    def check_knowledge(self):
        # The real sync call gets its own isolation tests below; the fixture
        # records the schedule and can inject a bounded failure.
        self.knowledge_calls += 1
        if self.fail_knowledge:
            raise RuntimeError("fixture knowledge sync failed")


class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote"
        self.remote.mkdir()
        shutil.copytree(
            ROOT / "plugins",
            self.remote / "plugins",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copy(ROOT / "product-contract.json", self.remote)
        shutil.copy(ROOT / "runtime-requirements.txt", self.remote)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        # This disposable repository is a local server fixture. New Git
        # versions may launch maintenance after commit as well as fetch;
        # detached writers must not outlive its test-owned directory.
        self.git("config", "maintenance.auto", "false")
        self.git("config", "gc.auto", "0")
        self.sha = self.commit("first")
        self.root = self.base / "updates"
        self.root.mkdir()
        self.config = self.base / "adapter.json"
        self.engine = self.base / "engine.json"
        self.admission = self.base / "adapter.admission.sqlite3"
        atomic(
            self.engine,
            dict(
                root=str(self.base / "data"),
                domain="test",
                agent_command=["old-worker"],
                admission_path=str(self.admission),
            ),
        )
        atomic(
            self.config,
            dict(
                python=sys.executable,
                engine_config=str(self.engine),
                admission_path=str(self.admission),
                runtime_scripts=str(SCRIPTS),
            ),
        )
        self.initial = read(self.config)
        self.settings = self.base / "updater.json"
        self.cache = (
            self.base / "codex/plugins/cache/mindie-agent/mindie-agent/old/scripts"
        )
        self.cache.mkdir(parents=True)
        (self.cache / "bridge.py").write_text("retained safe entrypoint", encoding="utf-8")
        initial_version = read(self.remote / "plugins/mindie-agent/.codex-plugin/plugin.json")["version"]
        (self.cache.parent.parent / initial_version / "scripts").mkdir(parents=True)
        atomic(
            self.root / "fixture-marketplace.json",
            dict(
                name="mindie-agent",
                root=str(self.remote),
                marketplaceSource=dict(sourceType="local"),
            ),
        )
        atomic(
            self.settings,
            dict(
                root=str(self.root),
                adapter_config=str(self.config),
                repository=str(self.remote),
                channel="main",
                python=sys.executable,
                codex="fixture-codex",
                uv="fixture-uv",
                codex_home=str(self.base / "codex"),
            ),
        )
        self.updater = LocalUpdater(self.settings)
        bind_explicit_config(None)

    def tearDown(self):
        bind_explicit_config(None)
        cleanup_temporary_directory(self.temp)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.remote), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def check(self):
        self.updater.state["next_check"] = 0
        atomic(self.updater.state_path, self.updater.state)
        return self.updater.check()

    def test_two_git_revisions_install_all_surfaces_and_leave_source_dirty(self):
        first = self.check()
        self.assertEqual(first["status"], "installed")
        old_plugin = Path(first["current"]["plugin"])
        self.assertEqual(first["current"]["revision"], self.sha)
        skill = self.remote / "plugins/mindie-agent/skills/mindie-agent/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nRevision two marker\n", encoding="utf-8")
        bridge = self.remote / "plugins/mindie-agent/scripts/bridge.py"
        bridge.write_text(bridge.read_text(encoding="utf-8") + "\n# Revision two marker\n", encoding="utf-8")
        second_sha = self.commit("second")
        skill.write_text(skill.read_text(encoding="utf-8") + "\nUncommitted developer work\n", encoding="utf-8")
        result = self.check()
        self.assertEqual(result["status"], "installed")
        plugin = Path(result["current"]["plugin"])
        self.assertEqual(result["current"]["revision"], second_sha)
        self.assertIn(
            "Revision two marker", (plugin / "skills/mindie-agent/SKILL.md").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "Uncommitted", (plugin / "skills/mindie-agent/SKILL.md").read_text(encoding="utf-8")
        )
        mcp = read(plugin / ".mcp.json")["mcpServers"]["mindie-knowledge"]
        self.assertEqual(mcp["args"][0], str(self.root / "runtime_launcher.py"))
        self.assertIn(
            str(self.root / "runtime_launcher.py"),
            read(plugin / "hooks/hooks.json")["hooks"]["Stop"][0]["hooks"][0][
                "command"
            ],
        )
        self.assertFalse(old_plugin.exists())
        self.assertFalse((self.cache / "bridge.py").exists())
        self.assertIn("Uncommitted", skill.read_text(encoding="utf-8"))
        installs = self.updater.installs
        self.assertEqual(self.check()["status"], "up_to_date")
        self.assertEqual(self.updater.installs, installs)

    def test_upgrade_from_core_without_handoff_api(self):
        """The previous core cannot import the new helper's lock_held API."""
        command = self.updater.command
        old_python = self.initial["python"]

        def old_runtime_lacks_api(args, **kwargs):
            if (len(args) > 2 and str(args[1]).endswith("service_handoff.py")
                    and args[2] == "stop" and str(args[0]) == old_python):
                raise RuntimeError("ImportError: cannot import name 'lock_held'")
            return command(args, **kwargs)

        with patch.object(self.updater, "command", side_effect=old_runtime_lacks_api):
            result = self.check()
        self.assertEqual(result["status"], "installed")
        self.assertNotEqual(read(self.config)["python"], old_python)

    def test_verify_native_rejects_same_version_stale_bytes(self):
        result = self.check()
        self.assertEqual(result["status"], "installed")
        current = self.updater.state["current"]
        version = current["version"]
        plugin = Path(current["plugin"])
        cache_dir = self.updater.native_cache() / version
        # The byte-identical resolved cache verifies.
        entry = self.updater.verify_native(version, str(plugin))
        self.assertEqual(entry["version"], version)
        # Imports into the packaged source create runtime bytecode after
        # native installation. It was never part of the shipped package.
        generated = plugin / "scripts/__pycache__/bridge.cpython-311.pyc"
        generated.parent.mkdir(exist_ok=True)
        generated.write_bytes(b"runtime-generated bytecode")
        self.assertEqual(self.updater.verify_native(version, str(plugin))["version"], version)
        # A stale same-version cache copy is a hard failure, never a warning.
        victim = cache_dir / "scripts/bridge.py"
        original = victim.read_bytes()
        victim.write_text("stale bytes from an older generation\n", encoding="utf-8")
        try:
            with self.assertRaises(RuntimeError):
                self.updater.verify_native(version, str(plugin))
        finally:
            victim.write_bytes(original)
        self.assertEqual(
            self.updater.verify_native(version, str(plugin))["version"], version
        )

    def test_summary_worker_moves_with_runtime_generation(self):
        engine = read(self.engine)
        engine['summary_command'] = ['old-python', str(SCRIPTS / 'agent_worker.py'), '--model', 'explicit-nonthinking-model']
        atomic(self.engine, engine)
        result = self.check()
        self.assertEqual(result['status'], 'installed')
        selected = read(self.config)
        updated = read(selected['engine_config'])
        self.assertEqual(updated['summary_command'], [selected['python'],
            str(Path(selected['runtime_scripts']) / 'agent_worker.py')])

    def test_legacy_model_arguments_are_replaced_by_owned_policy(self):
        engine = read(self.engine)
        engine['summary_command'] = ['old-python', str(SCRIPTS / 'agent_worker.py'),
                                     '--model', 'gpt-6-luna', '--reasoning-effort', 'low']
        atomic(self.engine, engine)
        result = self.check()
        self.assertEqual(result['status'], 'installed')
        selected = read(self.config)
        updated = read(selected['engine_config'])
        self.assertEqual(updated['summary_command'], [selected['python'],
            str(Path(selected['runtime_scripts']) / 'agent_worker.py')])

    def test_native_inventory_without_source_type_updates_owned_marketplace(self):
        first = self.check()
        self.assertEqual(first["status"], "installed")
        inventory = self.root / "fixture-marketplace.json"
        # Observed Windows codex-cli 0.158.0-alpha.2.1 list response.
        atomic(inventory, dict(name="mindie-agent", root=str(self.root / "marketplace")))
        worker = self.remote / "plugins/mindie-agent/scripts/agent_worker.py"
        worker.write_text(worker.read_text(encoding="utf-8") + "\n# updated worker\n", encoding="utf-8")
        revision = self.commit("updated worker")
        result = self.check()
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["current"]["revision"], revision)
        self.assertEqual(self.updater.verify_native(result["current"]["version"], result["current"]["plugin"])["version"], result["current"]["version"])

    def test_unknown_external_marketplace_rejected_before_service_stop(self):
        external = dict(name="mindie-agent", root=str(self.remote))
        with patch.object(self.updater, 'marketplace', return_value=external), patch.object(self.updater, 'command') as command:
            with self.assertRaises(auto_update.Incompatible):
                self.updater.install(dict(revision='candidate'))
        command.assert_not_called()
        self.assertFalse((self.root / 'transaction.json').exists())

    def test_package_binds_installation_config_into_mcp_and_hook(self):
        result = self.check()
        self.assertEqual(result["status"], "installed")
        plugin = Path(result["current"]["plugin"])
        expected = str(self.config.expanduser().absolute())
        mcp = read(plugin / ".mcp.json")
        for name, server in mcp["mcpServers"].items():
            with self.subTest(server=name):
                self.assertEqual(server["env"]["MINDIE_AGENT_CONFIG"], expected)
                self.assertEqual(list(server["env"]), ["MINDIE_AGENT_CONFIG"])
                self.assertNotIn("env_vars", server)
        command = read(plugin / "hooks/hooks.json")["hooks"]["Stop"][0]["hooks"][0][
            "command"
        ]
        self.assertIn("--config", command)
        self.assertIn(expected, command)
        self.assertIn("stop", command)
        hook = read(plugin / "hooks/hooks.json")["hooks"]["Stop"][0]["hooks"][0]
        self.assertNotIn("timeout", hook)
        other = str(self.base / "other-adapter.json")
        import bridge as bridge_mod
        from session_gate import config_path as live_config

        bind_explicit_config(None)
        try:
            with patch.dict(os.environ, {"MINDIE_AGENT_CONFIG": other}, clear=False):
                rest = bridge_mod._optional_config_prefix(["--config", expected, "stop"])
                self.assertEqual(rest, ["stop"])
                self.assertEqual(str(live_config()), expected)
                self.assertEqual(os.environ["MINDIE_AGENT_CONFIG"], expected)
        finally:
            bind_explicit_config(None)
        binding = read(plugin / "scripts/installation.json")
        self.assertEqual(binding, {"adapter_config": expected})
        self.assertEqual(
            (plugin / "scripts/windows_process.py").read_bytes(),
            (SCRIPTS / "windows_process.py").read_bytes(),
        )

    def test_generated_bridge_init_binds_installation_config_without_env(self):
        result = self.check()
        self.assertEqual(result["status"], "installed")
        plugin = Path(result["current"]["plugin"])
        expected = str(self.config.expanduser().absolute())
        # LocalUpdater intentionally creates a dummy venv. This subprocess
        # checks actual dispatch, so bind the reviewed test interpreter.
        adapter = read(self.config)
        adapter["python"] = sys.executable
        atomic(self.config, adapter)
        home = self.base / "clean-home"
        (home / ".config/mindie-agent").mkdir(parents=True)
        decoy = home / ".config/mindie-agent/codex.json"
        decoy.write_text(
            json.dumps(
                dict(
                    python="decoy",
                    engine_config=str(self.base / "decoy-engine.json"),
                    admission_path=str(self.base / "decoy.admission.sqlite3"),
                    runtime_scripts=str(plugin / "scripts"),
                )
            )
        , encoding="utf-8")
        other = self.base / "other-adapter.json"
        atomic(
            other,
            dict(
                python=sys.executable,
                engine_config=str(self.engine),
                admission_path=str(self.admission),
                runtime_scripts=str(plugin / "scripts"),
            ),
        )
        env = {
            "HOME": str(home),
            # pathlib.Path.home() uses USERPROFILE on Windows; overriding only
            # HOME leaves subprocesses pointed at the real user profile.
            "USERPROFILE": str(home),
            "PATH": os.environ.get("PATH", ""),
            "TMPDIR": str(self.base / "tmp"),
        }
        for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            if name in os.environ:
                env[name] = os.environ[name]
        (self.base / "tmp").mkdir(exist_ok=True)
        bridge = plugin / "scripts/bridge.py"
        init = subprocess.run(
            [sys.executable, str(bridge), "init"],
            text=True,
            capture_output=True,
            timeout=15,
            env=env,
        )
        self.assertEqual(init.returncode, 0, init.stderr or init.stdout)
        payload = json.loads(init.stdout)
        self.assertEqual(payload["adapter"]["config"], expected)
        status = subprocess.run(
            [sys.executable, str(bridge), "status"],
            text=True,
            capture_output=True,
            timeout=15,
            env={**env, "MINDIE_AGENT_CONFIG": str(decoy)},
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["adapter"]["config"], expected)
        prefixed = subprocess.run(
            [sys.executable, str(bridge), "--config", str(other), "status"],
            text=True,
            capture_output=True,
            timeout=15,
            env={**env, "MINDIE_AGENT_CONFIG": str(decoy)},
        )
        self.assertEqual(prefixed.returncode, 0, prefixed.stderr)
        self.assertEqual(
            json.loads(prefixed.stdout)["adapter"]["config"],
            str(other.expanduser().absolute()),
        )

    def test_product_community_projection_restores_only_owned_field(self):
        import consent
        community = consent.shared_community_path_for(self.config)
        declaration, _ = product_contract.product(ROOT)
        previous = dict(repository=declaration["publication"]["repository"],
                        publication_contract_sha256="b" * 64, enabled=False,
                        sibling={"value": "retained"})
        atomic(community, previous)
        atomic(self.root / "transaction.json", dict(candidate=self.sha, adapter=self.initial))
        self.updater.project_publication(declaration)
        projected = read(community)
        self.assertEqual(projected["publication_contract_sha256"], declaration["publication"]["contract_sha256"])
        projected["sibling"] = {"value": "changed concurrently"}
        atomic(community, projected)
        self.updater.restore_publication(read(self.root / "transaction.json"))
        restored = read(community)
        self.assertEqual(restored["publication_contract_sha256"], previous["publication_contract_sha256"])
        self.assertEqual(restored["sibling"], {"value": "changed concurrently"})
        self.assertIs(restored["enabled"], False)

    def test_product_community_projection_preserves_foreign_repository_and_conflicts(self):
        import consent
        community = consent.shared_community_path_for(self.config)
        declaration, _ = product_contract.product(ROOT)
        atomic(community, dict(repository="owner/custom", publication_contract_sha256="b" * 64))
        before = community.read_bytes()
        atomic(self.root / "transaction.json", dict(candidate=self.sha, adapter=self.initial))
        self.updater.project_publication(declaration)
        self.assertEqual(community.read_bytes(), before)
        self.assertNotIn("publication_projection", read(self.root / "transaction.json"))
        atomic(community, dict(repository=declaration["publication"]["repository"]))
        self.updater.project_publication(declaration)
        current = read(community)
        current["publication_contract_sha256"] = "c" * 64
        atomic(community, current)
        before = community.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed during product rollback"):
            self.updater.restore_publication(read(self.root / "transaction.json"))
        self.assertEqual(community.read_bytes(), before)

    def test_install_failure_and_conflicting_rollback_remain_distinct_and_durable(self):
        import consent
        community = consent.shared_community_path_for(self.config)
        declaration, _ = product_contract.product(ROOT)
        atomic(community, dict(repository=declaration["publication"]["repository"],
                               publication_contract_sha256="b" * 64))
        original = OSError("ORIGINAL_INSTALL_FAILURE_SENTINEL")
        project = self.updater.project_publication
        install = self.updater.install
        observed = []
        def conflicting_projection(value):
            project(value)
            current = read(community)
            current["publication_contract_sha256"] = "c" * 64
            atomic(community, current)
            raise original
        def observe(candidate):
            try:
                return install(candidate)
            except auto_update.InstallRollbackError as exc:
                observed.append(exc)
                raise
        with patch.object(self.updater, "project_publication", side_effect=conflicting_projection), \
             patch.object(self.updater, "install", side_effect=observe):
            result = self.check()
        self.assertEqual(result["status"], "update_failed")
        self.assertIn("install failed (OSError)", result["error"])
        self.assertIn("rollback failed (RuntimeError)", result["error"])
        durable = read(self.updater.state_path)
        self.assertEqual(durable["original_install_error"], dict(
            stage="install", error="OSError", message="ORIGINAL_INSTALL_FAILURE_SENTINEL"))
        self.assertEqual(durable["rollback_error"]["stage"], "rollback")
        self.assertIn("publication contract changed", durable["rollback_error"]["message"])
        self.assertEqual(len(observed), 1)
        self.assertIs(observed[0].__cause__, original)
        self.assertIsInstance(observed[0].rollback_exception, RuntimeError)
        self.assertIn("Rollback also failed: RuntimeError", original.__notes__)
        self.assertEqual(read(community)["publication_contract_sha256"], "c" * 64)
        self.assertTrue((self.root / "transaction.json").is_file())
        self.assertEqual(self.updater.installs, 1)  # no retry after the uncertain rollback
        self.assertNotIn("next_retry_at", result["attempts"][self.sha])

    def test_candidate_failure_stage_and_code_reach_durable_update_result(self):
        error = product_contract.CandidateValidationError(dict(
            stage="runtime_pins", code="revision_mismatch", component="mindie-knowledge"))
        with patch.object(self.updater, "probe_runtime", side_effect=error):
            result = self.check()
        self.assertEqual(result["status"], "update_failed")
        self.assertIn("runtime_pins: revision_mismatch (mindie-knowledge)", result["error"])
        self.assertEqual(read(self.updater.state_path)["error"], result["error"])
        self.assertEqual(self.updater.installs, 0)
        self.assertEqual(read(self.config), self.initial)

    def test_prepared_plugin_edit_prevents_native_installation(self):
        candidate = self.updater.prepare(self.sha)
        script = Path(candidate["plugin"]) / "scripts/bridge.py"
        script.write_text(script.read_text() + "\n# corrupted packaged entry\n")
        with self.assertRaisesRegex(auto_update.Incompatible, "plugin bytes changed"):
            self.updater.install(candidate)
        self.assertEqual(read(self.config), self.initial)
        self.assertEqual(self.updater.installs, 0)
        self.assertFalse((self.root / "transaction.json").exists())

    def test_cached_candidate_runtime_drift_is_rechecked_before_native_installation(self):
        candidate = self.updater.prepare(self.sha)
        reused = self.updater.prepare(self.sha)
        self.assertEqual(candidate, reused)
        def changed_runtime(*args, **kwargs):
            self.assertEqual(kwargs["verified_receipt"], candidate["validation"])
            candidate_validate.installed_revisions(candidate["validation"]["runtime"])
        distribution = SimpleNamespace(read_text=lambda _: '{"vcs_info":{"commit_id":"changed"}}')
        with patch.object(self.updater, "probe_runtime", side_effect=changed_runtime), \
             patch.object(candidate_validate.importlib.metadata, "distribution", return_value=distribution):
            with self.assertRaisesRegex(ValueError, "revision_mismatch"):
                self.updater.install(reused)
        self.assertEqual(read(self.config), self.initial)
        self.assertEqual(self.updater.installs, 0)
        self.assertFalse((self.root / "transaction.json").exists())

    def test_prepared_source_edit_prevents_installation(self):
        candidate = self.updater.prepare(self.sha)
        source = Path(candidate["source"])
        script = source / "plugins/mindie-agent/scripts/candidate_validate.py"
        script.write_text(script.read_text() + "\n# concurrent change\n")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.updater.install(candidate)
        self.assertEqual(read(self.config), self.initial)
        self.assertFalse((self.root / "transaction.json").exists())

    def test_missing_contract_never_replaces_local_safety_fix(self):
        (self.remote / "product-contract.json").unlink()
        sha = self.commit("old unsafe main")
        for _ in range(5):
            result = self.check()
            self.assertEqual(result["status"], "waiting_for_compatible_source")
        self.assertEqual(result["attempts"][sha]["count"], 1)
        self.assertEqual(read(self.config), self.initial)
        self.assertEqual(self.updater.installs, 0)

    def test_idle_authorization_does_not_block_update(self):
        with patch.dict(os.environ, CODEX_THREAD_ID="fixture-manual"):
            sessions = Sessions(self.config)
            lease = sessions.activate()
            result = self.check()
            self.assertEqual(result["status"], "installed")
            with closing(sqlite3.connect(self.admission)) as db, db:
                row = db.execute(
                    "SELECT session, enabled, token FROM leases WHERE session=?",
                    ("fixture-manual",),
                ).fetchone()
            self.assertEqual(row[0], "fixture-manual")
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2], lease["mindie_activation"])
            adapter = read(self.config)
            engine = read(adapter["engine_config"])
            self.assertNotIn("session_activation", engine)
            self.assertTrue(
                Path(engine["transcript_adapter"]).is_file()
            )
            self.assertTrue(engine["transcript_adapter"].endswith("codex_transcript.py"))
            self.assertTrue(Path(adapter["runtime_scripts"]).is_dir())
            self.assertEqual(
                Path(adapter["runtime_scripts"]),
                Path(result["current"]["plugin"]) / "scripts",
            )

    def test_inflight_call_lock_defers_update_and_activation(self):
        with update_lock(self.config):
            self.assertEqual(self.check()["status"], "waiting_for_idle")
        with update_lock(self.config, exclusive=True):
            with patch.dict(os.environ, CODEX_THREAD_ID="fixture-manual"):
                with self.assertRaises(BlockingIOError):
                    Sessions(self.config).activate()
        self.assertEqual(self.check()["status"], "installed")

    def test_failed_install_rolls_back_and_stops_after_three_attempts(self):
        for _ in range(3):
            self.updater.fail_install = True
            self.assertEqual(self.check()["status"], "update_failed")
            self.assertEqual(read(self.config), self.initial)
            self.assertEqual(
                read(self.root / "fixture-marketplace.json")["root"], str(self.remote)
            )
            self.assertFalse((self.cache / "bridge.py").exists())
            self.assertFalse((self.root / "transaction.json").exists())
        count = self.updater.installs
        self.assertEqual(self.check()["status"], "attempts_exhausted")
        self.assertEqual(self.updater.installs, count)

    def test_uncertain_crash_consumes_attempt_and_recovers_before_next_check(self):
        original = self.updater.command

        def crash(args, **kwargs):
            if [str(a) for a in args][:3] == ["fixture-codex", "plugin", "add"]:
                raise KeyboardInterrupt("simulated process loss")
            return original(args, **kwargs)

        with patch.object(self.updater, "command", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.check()
        self.assertEqual(
            read(self.updater.state_path)["attempts"][self.sha]["count"], 1
        )
        self.assertTrue((self.root / "transaction.json").exists())
        self.assertEqual(self.check()["status"], "installed")
        self.assertEqual(self.updater.state["attempts"][self.sha]["count"], 2)

    def test_release_channel_resolves_annotated_tag_to_exact_commit(self):
        self.git("tag", "-a", "v1.0.0", "-m", "release")
        self.updater.settings["channel"] = "release"
        with patch(
            "auto_update.urllib.request.urlopen",
            return_value=io.BytesIO(b'{"tag_name":"v1.0.0"}'),
        ):
            self.assertEqual(self.updater.resolve(), self.sha)

    def test_remote_only_update_retains_identical_stop_command(self):
        first = self.check()
        before = read(Path(first["current"]["plugin"]) / "hooks/hooks.json")
        remote = self.remote / "plugins/mindie-agent/scripts/remote_bridge.py"
        remote.write_text(remote.read_text(encoding="utf-8") + "\n# Remote-only revision\n", encoding="utf-8")
        self.commit("remote-only")
        second = self.check()
        self.assertEqual(second["status"], "installed")
        self.assertEqual(read(Path(second["current"]["plugin"]) / "hooks/hooks.json"), before)

    def test_stop_helper_change_keeps_stable_command_and_selects_new_generation(self):
        first = self.check()
        previous = Path(first["current"]["plugin"])
        previous_command = read(previous / "hooks/hooks.json")["hooks"]["Stop"][0][
            "hooks"
        ][0]["command"]
        self.assertIn(str(self.root / "runtime_launcher.py"), previous_command)
        for name in ("diagnostic_support.py", "diagnostic_fallback.py", "windows_process.py"):
            path = self.remote / "plugins/mindie-agent/scripts" / name
            path.write_text(path.read_text(encoding="utf-8") + f"\n# {name} stop behavior\n", encoding="utf-8")
            self.commit(name)
            result = self.check()
            self.assertEqual(result["status"], "installed")
            plugin = Path(result["current"]["plugin"])
            command = read(plugin / "hooks/hooks.json")["hooks"]["Stop"][0]["hooks"][
                0
            ]["command"]
            self.assertEqual(command, previous_command)
            self.assertIn(f"# {name} stop behavior", (plugin / "scripts" / name).read_text())
            self.assertEqual(read(self.config)["runtime_scripts"], str(plugin / "scripts"))
            previous, previous_command = plugin, command

    def test_wrapper_change_replaces_stop_even_with_identical_helpers(self):
        first = self.check()
        previous = Path(first["current"]["plugin"])
        hook_path = previous / "hooks/hooks.json"
        legacy = read(hook_path)
        legacy["hooks"]["Stop"][0]["hooks"][0]["commandWindows"] = (
            'python "legacy-bridge.py" stop >NUL 2>&1 & echo {}'
        )
        atomic(hook_path, legacy)
        remote = self.remote / "plugins/mindie-agent/scripts/remote_bridge.py"
        remote.write_text(remote.read_text(encoding="utf-8") + "\n# Next revision\n", encoding="utf-8")
        self.commit("wrapper migration")
        result = self.check()
        self.assertEqual(result["status"], "installed")
        plugin = Path(result["current"]["plugin"])
        hook = read(plugin / "hooks/hooks.json")["hooks"]["Stop"][0]["hooks"][0]
        self.assertIn(str(self.root / "runtime_launcher.py"), hook["command"])
        self.assertIn("-EncodedCommand", hook["commandWindows"])
        self.assertNotEqual(hook["commandWindows"], legacy["hooks"]["Stop"][0]["hooks"][0]["commandWindows"])

    def test_native_cache_is_not_duplicated_or_restored_by_updater(self):
        self.check()
        self.assertFalse(self.cache.exists())
        self.assertFalse((self.root / "retained-caches").exists())
        self.assertFalse((self.root / "legacy-caches-original").exists())

    def test_knowledge_sync_runs_on_every_schedule_and_failure_is_isolated(self):
        first = self.check()
        self.assertEqual(first["status"], "installed")
        self.assertEqual(self.updater.knowledge_calls, 1)
        # Plugin up to date, attempts exhausted and resolve failures all still
        # run the knowledge sync: the plugin build never blocks it.
        self.assertEqual(self.check()["status"], "up_to_date")
        self.updater.fail_knowledge = True
        result = self.check()
        self.assertEqual(result["status"], "up_to_date")
        self.assertEqual(result["knowledge_status"], "sync_failed")
        self.assertIn("knowledge_error", result)
        self.updater.fail_knowledge = False
        self.remote.joinpath("marker").write_text("new revision", encoding="utf-8")
        self.commit("failing candidate")
        for _ in range(3):
            self.updater.fail_install = True
            self.assertEqual(self.check()["status"], "update_failed")
        self.assertEqual(self.check()["status"], "attempts_exhausted")
        self.assertGreaterEqual(self.updater.knowledge_calls, 6)

    def test_unreadable_admission_bytes_do_not_block_update(self):
        sessions = Sessions(self.config)
        sessions.path.write_text("not a sqlite database", encoding="utf-8")
        result = self.check()
        self.assertEqual(result["status"], "installed")
        self.assertEqual(sessions.path.read_text(encoding="utf-8"), "not a sqlite database")
        self.assertGreater(self.updater.installs, 0)

    def test_install_verifies_native_selection_while_retaining_old_entrypoints(self):
        # Root's macOS scenario: a retained cache from the 20-digit timestamp
        # era coexists with the candidate. The candidate must win natively AND
        # the old entrypoint must keep its exact bytes and path.
        old = self.updater.native_cache() / "0.1.0+codex.20260919061330608250/scripts"
        old.mkdir(parents=True)
        (old / "bridge.py").write_text("old loaded-task entrypoint", encoding="utf-8")
        result = self.check()
        self.assertEqual(result["status"], "installed")
        candidate_version = result["current"]["version"]
        self.assertGreater(
            int(candidate_version.rsplit(".", 1)[1]), 20260919061330608250
        )
        native = self.updater.native_list()["installed"][0]
        self.assertEqual(native["version"], candidate_version)
        self.assertTrue(native["installed"] and native["enabled"])
        retained = self.updater.native_cache() / "0.1.0+codex.20260919061330608250/scripts/bridge.py"
        self.assertFalse(retained.exists())

    def test_wrong_native_selection_is_visible_and_rolls_back(self):
        # Inject an incorrect native readback after add, independently of the
        # host's own cache retention/pruning policy.
        original = self.updater.native_list
        reads = 0
        def wrong_selection_once():
            nonlocal reads
            value = original()
            reads += 1
            if reads == 2:
                value["installed"][0]["version"] = "0.0.0"
            return value
        with patch.object(self.updater, "native_list", side_effect=wrong_selection_once):
            result = self.check()
        self.assertEqual(result["status"], "update_failed")
        self.assertIn("native plugin selection", result["error"])
        self.assertNotIn("current", self.updater.state)
        self.assertEqual(read(self.config), self.initial)
        self.assertFalse((self.root / "transaction.json").exists())
        self.assertEqual(original()["installed"][0]["version"],
                         read(self.remote / "plugins/mindie-agent/.codex-plugin/plugin.json")["version"])

    def test_fetch_deadline_and_network_backoff_do_not_spawn_models(self):
        with patch.object(
            self.updater, "resolve", side_effect=TimeoutError("network")
        ) as resolve:
            for _ in range(3):
                result = self.check()
            self.assertGreater(result["next_check"], time.time() + 3500)
            self.updater.check()
            self.assertEqual(resolve.call_count, 3)
        self.updater.deadline = time.monotonic() - 1  # not an execution cap
        self.assertEqual(self.updater.command(
            [sys.executable, "-c", "import time; time.sleep(.1); print('complete')"]
        ), "complete\n")


    def test_generation_cleanup_requires_state_and_matching_runtime_pointer(self):
        first = self.check()
        generation = Path(first["current"]["plugin"]).parent
        saved = self.updater.state_path.read_bytes()
        self.updater.state_path.unlink()
        self.assertEqual(self.updater.collect_generations()["status"], "failed")
        self.assertTrue(generation.exists())
        self.updater.state_path.write_bytes(saved)
        state = read(self.updater.state_path)
        state["current"]["revision"] = "different"
        atomic(self.updater.state_path, state)
        self.assertEqual(self.updater.collect_generations()["status"], "failed")
        self.assertTrue(generation.exists())

    def test_generation_cleanup_retains_active_lease_and_bounds_inactive_storage(self):
        from update_lock import file_lock
        self.check()
        generations = self.root / "generations"
        live = generations / "leased"
        for index in range(20):
            name = "leased" if index == 0 else f"retired-{index}"
            directory = generations / name
            directory.mkdir()
            atomic(directory / "ownership.json", {"schema": "mindie-runtime-generation/2", "revision": name})
            (directory / "data").write_bytes(b"x" * 4096)
        locks = self.root / "generation-locks"
        with file_lock(locks / "leased.lock"):
            receipt = self.updater.collect_generations()
            self.assertEqual(len(receipt["removed"]), 19)
            self.assertTrue(live.exists())
            self.assertEqual(len(list(generations.iterdir())), 2)
        receipt = self.updater.collect_generations()
        self.assertEqual(receipt["removed"], ["leased"])
        self.assertEqual(len(list(generations.iterdir())), 1)
        self.assertFalse((locks / "leased.lock").exists())

    def test_generation_cleanup_keeps_untracked_and_transaction_roots(self):
        self.check()
        untracked = self.root / "generations" / "untracked"
        untracked.mkdir()
        (untracked / "user-content").write_text("preserve")
        receipt = self.updater.collect_generations()
        self.assertEqual(receipt["status"], "untracked_retained")
        self.assertTrue(untracked.exists())
        atomic(self.root / "transaction.json", {"stage": "unresolved"})
        self.assertEqual(self.updater.collect_generations()["reason"], "transaction_pending")

    def test_cleanup_failure_is_visible_without_reverting_completed_install(self):
        with patch.object(self.updater, "collect_generations", return_value={"status": "failed", "error_type": "PermissionError"}):
            result = self.check()
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["retention"]["status"], "failed")
        self.assertEqual(read(self.updater.state_path)["retention"], result["retention"])

    def test_committed_generation_publishes_stable_launcher(self):
        with patch("auto_update.schedule_enable") as schedule:
            result = self.check()
        schedule.assert_not_called()
        self.assertEqual(result["status"], "installed")
        plugin = Path(result["current"]["plugin"])
        hooks = (plugin / "hooks/hooks.json").read_bytes()
        bridge = (plugin / "scripts/bridge.py").read_bytes()
        launcher = self.root / "launcher.py"
        self.assertEqual(
            launcher.read_bytes(),
            (plugin / "scripts/update_launcher.py").read_bytes(),
        )
        self.assertIn(b"unsupported launcher operation", launcher.read_bytes())
        self.check()
        self.assertEqual((plugin / "hooks/hooks.json").read_bytes(), hooks)
        self.assertEqual((plugin / "scripts/bridge.py").read_bytes(), bridge)
        before = launcher.read_bytes()
        state = read(self.updater.state_path)
        state["current"]["plugin"] = str(self.base / "outside-plugin")
        state["next_check"] = 0
        atomic(self.updater.state_path, state)
        failed = self.updater.check()
        self.assertEqual(failed["status"], "check_failed")
        self.assertIn("invalid current", failed["error"])
        self.assertEqual(launcher.read_bytes(), before)
        self.assertFalse((self.root / "launcher.next").exists())

    def test_manual_enable_installs_native_plugin_and_persists_manual_state(self):
        args = SimpleNamespace(
            source_root=self.remote,
            root=self.root,
            settings=self.settings,
            channel="main",
            schedule="manual",
        )
        with (
            patch("auto_update.Updater", LocalUpdater),
            patch("auto_update.schedule_enable") as schedule,
        ):
            result = auto_update.enable(args)
        schedule.assert_not_called()
        self.assertEqual(result["status"], "manual")
        self.assertEqual(result["schedule"]["mode"], "manual")
        self.assertIs(result["schedule"]["registered"], False)
        launcher = Path(result["schedule"]["check_command"][1])
        self.assertTrue(launcher.is_file())
        settings = read(self.settings)
        self.assertEqual(settings["schedule_mode"], "manual")
        self.assertEqual(settings["schedule"], result["schedule"])
        state = read(self.root / "state.json")
        current = state["current"]
        verified = LocalUpdater(self.settings).verify_native(
            current["version"], current["plugin"]
        )
        self.assertEqual(verified["version"], current["version"])


class UpdateIdleTests(unittest.TestCase):
    def test_missing_service_is_idle(self):
        import service_handoff

        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.json"
            engine.write_text(json.dumps(dict(root=tmp, domain="test", capture_mode="public-transcript",
                transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
                redactor_executable=str(Path(tmp) / "gitleaks"))), encoding="utf-8")
            self.assertTrue(service_handoff.stop(str(engine)))

    def test_absent_stop_if_idle_fails_closed(self):
        import service_handoff

        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.json"
            engine.write_text(json.dumps(dict(root=tmp, domain="test", capture_mode="public-transcript",
                transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
                redactor_executable=str(Path(tmp) / "gitleaks"))), encoding="utf-8")
            with (
                patch(
                    "service_handoff.connect",
                    return_value=dict(url="http://127.0.0.1:9", token="t"),
                ),
                patch("service_handoff.rpc", return_value=dict(status="ok")),
            ):
                with self.assertRaises(RuntimeError):
                    service_handoff.stop(str(engine))

    # Authenticated acknowledgement and real lifetime release are exercised
    # by test_service_handoff; a canned sequence of TCP results cannot prove it.


if __name__ == "__main__":
    unittest.main()
