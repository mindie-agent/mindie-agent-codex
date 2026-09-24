"""Exercise two real Git revisions without network, models or a real Codex install."""

import json
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
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
                # directories and caches exactly the marketplace version.
                cache = self.native_cache()
                for entry in cache.iterdir():
                    if entry.is_dir():
                        shutil.rmtree(entry)
                marketplace = read(self.root / "fixture-marketplace.json")
                manifest = read(
                    Path(marketplace["root"])
                    / "plugins/mindie-agent/.codex-plugin/plugin.json"
                )
                scripts = cache / manifest["version"] / "scripts"
                scripts.mkdir(parents=True)
                (scripts / "bridge.py").write_text("cached entrypoint")
                if self.fail_install:
                    self.fail_install = False  # Rollback CLI succeeds.
                    raise RuntimeError("fixture installation failed")
            elif args[1:3] == ["plugin", "list"]:
                return json.dumps(self.native_list())
            return "{}"
        if args[0] == "fixture-uv":
            if args[1] == "venv":
                runtime = Path(args[-1]) / "bin/python"
                runtime.parent.mkdir(parents=True)
                runtime.touch()
                self.builds += 1
            return ""
        if len(args) > 1 and args[1].endswith("service_handoff.py"):
            return json.dumps(dict(idle=self.idle))
        return super().command(args, **kwargs)

    def probe_runtime(self, python):
        self.assert_runtime = Path(python).exists()
        if not self.assert_runtime:
            raise RuntimeError("runtime missing")

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
        shutil.copy(ROOT / "update-contract.json", self.remote)
        shutil.copy(ROOT / "runtime-requirements.txt", self.remote)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
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
        (self.cache / "bridge.py").write_text("retained safe entrypoint")
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
        self.temp.cleanup()

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
        skill.write_text(skill.read_text() + "\nRevision two marker\n")
        bridge = self.remote / "plugins/mindie-agent/scripts/bridge.py"
        bridge.write_text(bridge.read_text() + "\n# Revision two marker\n")
        second_sha = self.commit("second")
        skill.write_text(skill.read_text() + "\nUncommitted developer work\n")
        result = self.check()
        self.assertEqual(result["status"], "installed")
        plugin = Path(result["current"]["plugin"])
        self.assertEqual(result["current"]["revision"], second_sha)
        self.assertIn(
            "Revision two marker", (plugin / "skills/mindie-agent/SKILL.md").read_text()
        )
        self.assertNotIn(
            "Uncommitted", (plugin / "skills/mindie-agent/SKILL.md").read_text()
        )
        mcp = read(plugin / ".mcp.json")["mcpServers"]["mindie-knowledge"]
        self.assertEqual(mcp["args"][0], str(plugin / "scripts/bridge.py"))
        self.assertIn(
            str(plugin),
            read(plugin / "hooks/hooks.json")["hooks"]["Stop"][0]["hooks"][0][
                "command"
            ],
        )
        self.assertTrue(old_plugin.exists())
        self.assertTrue((self.cache / "bridge.py").exists())
        self.assertIn("Uncommitted", skill.read_text())
        installs = self.updater.installs
        self.assertEqual(self.check()["status"], "up_to_date")
        self.assertEqual(self.updater.installs, installs)

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
        self.assertEqual(hook["timeout"], 2)
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
        )
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
            "PATH": os.environ.get("PATH", ""),
            "TMPDIR": str(self.base / "tmp"),
        }
        (self.base / "tmp").mkdir(exist_ok=True)
        bridge = plugin / "scripts/bridge.py"
        init = subprocess.run(
            [sys.executable, str(bridge), "init"],
            text=True,
            capture_output=True,
            timeout=15,
            env=env,
        )
        self.assertEqual(init.returncode, 0, init.stderr)
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

    def test_missing_contract_never_replaces_local_safety_fix(self):
        (self.remote / "update-contract.json").unlink()
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
            with sqlite3.connect(self.admission) as db:
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
            self.assertTrue((self.cache / "bridge.py").exists())
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
        remote.write_text(remote.read_text() + "\n# Remote-only revision\n")
        self.commit("remote-only")
        second = self.check()
        self.assertEqual(second["status"], "installed")
        self.assertEqual(read(Path(second["current"]["plugin"]) / "hooks/hooks.json"), before)

    def test_stop_diagnostic_helper_change_selects_new_stop_command(self):
        first = self.check()
        previous = Path(first["current"]["plugin"])
        previous_command = read(previous / "hooks/hooks.json")["hooks"]["Stop"][0][
            "hooks"
        ][0]["command"]
        self.assertIn(str(previous / "scripts/bridge.py"), previous_command)
        for name in ("diagnostic_support.py", "diagnostic_fallback.py"):
            path = self.remote / "plugins/mindie-agent/scripts" / name
            path.write_text(path.read_text() + f"\n# {name} stop behavior\n")
            self.commit(name)
            result = self.check()
            self.assertEqual(result["status"], "installed")
            plugin = Path(result["current"]["plugin"])
            command = read(plugin / "hooks/hooks.json")["hooks"]["Stop"][0]["hooks"][
                0
            ]["command"]
            self.assertIn(str(plugin / "scripts/bridge.py"), command)
            self.assertNotIn(str(previous / "scripts/bridge.py"), command)
            self.assertNotEqual(command, previous_command)
            previous, previous_command = plugin, command

    def test_uncoordinated_caches_are_retained_untouched(self):
        # No compatibility shim: cached entrypoints of loaded tasks keep their
        # exact bytes; the updater only retains/restores them across switches.
        self.check()
        self.assertEqual((self.cache / "bridge.py").read_text(), "retained safe entrypoint")
        retained = self.root / "retained-caches/old/scripts/bridge.py"
        self.assertEqual(retained.read_text(), "retained safe entrypoint")
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
        self.remote.joinpath("marker").write_text("new revision")
        self.commit("failing candidate")
        for _ in range(3):
            self.updater.fail_install = True
            self.assertEqual(self.check()["status"], "update_failed")
        self.assertEqual(self.check()["status"], "attempts_exhausted")
        self.assertGreaterEqual(self.updater.knowledge_calls, 6)

    def test_unreadable_admission_bytes_do_not_block_update(self):
        sessions = Sessions(self.config)
        sessions.path.write_text("not a sqlite database")
        result = self.check()
        self.assertEqual(result["status"], "installed")
        self.assertEqual(sessions.path.read_text(), "not a sqlite database")
        self.assertGreater(self.updater.installs, 0)

    def test_install_verifies_native_selection_while_retaining_old_entrypoints(self):
        # Root's macOS scenario: a retained cache from the 20-digit timestamp
        # era coexists with the candidate. The candidate must win natively AND
        # the old entrypoint must keep its exact bytes and path.
        old = self.updater.native_cache() / "0.1.0+codex.20260919061330608250/scripts"
        old.mkdir(parents=True)
        (old / "bridge.py").write_text("old loaded-task entrypoint")
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
        self.assertEqual(retained.read_text(), "old loaded-task entrypoint")

    def test_no_fake_installed_when_retained_cache_wins_native_discovery(self):
        # A candidate whose build metadata sorts BELOW a retained cache (the
        # exact root failure: 14-digit new vs 20-digit old) must NOT report
        # installed: readback verification fails and rolls back.
        old = self.updater.native_cache() / "0.1.0+codex.20260919061330608250/scripts"
        old.mkdir(parents=True)
        (old / "bridge.py").write_text("old loaded-task entrypoint")
        with patch("auto_update.datetime") as clock:
            clock.now.return_value.strftime.return_value = "20260920055301"
            result = self.check()
        self.assertEqual(result["status"], "update_failed")
        self.assertIn("native plugin selection", result["error"])
        self.assertNotIn("current", self.updater.state)
        self.assertEqual(read(self.config), self.initial)
        self.assertFalse((self.root / "transaction.json").exists())
        # Rollback leaves the truthful previous native state, not the
        # rejected candidate masquerading as installed.
        native = self.updater.native_list()["installed"][0]
        self.assertEqual(native["version"], "0.1.0+codex.20260919061330608250")
        self.assertEqual(
            (self.updater.native_cache() / "0.1.0+codex.20260919061330608250/scripts/bridge.py").read_text(),
            "old loaded-task entrypoint",
        )
        # Bounded: a repeated check consumes attempts, never loops adds.
        with patch("auto_update.datetime") as clock:
            clock.now.return_value.strftime.return_value = "20260920055301"
            result = self.check()
        self.assertEqual(result["status"], "update_failed")

    def test_fetch_deadline_and_network_backoff_do_not_spawn_models(self):
        with patch.object(
            self.updater, "resolve", side_effect=TimeoutError("network")
        ) as resolve:
            for _ in range(3):
                result = self.check()
            self.assertGreater(result["next_check"], time.time() + 3500)
            self.updater.check()
            self.assertEqual(resolve.call_count, 3)
        self.updater.deadline = time.monotonic() - 1
        with self.assertRaises(TimeoutError):
            self.updater.command(
                [sys.executable, "-c", "raise AssertionError('must not run')"]
            )


class UpdateIdleTests(unittest.TestCase):
    def test_missing_service_is_idle(self):
        import service_handoff

        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.json"
            engine.write_text(json.dumps(dict(root=tmp, domain="test")))
            self.assertTrue(service_handoff.stop(str(engine)))

    def test_absent_stop_if_idle_fails_closed(self):
        import service_handoff

        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.json"
            engine.write_text(json.dumps(dict(root=tmp, domain="test")))
            with (
                patch(
                    "service_handoff.connect",
                    return_value=dict(url="http://127.0.0.1:9", token="t"),
                ),
                patch("service_handoff.rpc", return_value=dict(status="ok")),
            ):
                with self.assertRaises(RuntimeError):
                    service_handoff.stop(str(engine))

    def test_authenticated_idle_result_is_used(self):
        import service_handoff

        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.json"
            engine.write_text(json.dumps(dict(root=tmp, domain="test")))
            with (
                patch(
                    "service_handoff.connect",
                    return_value=dict(url="http://127.0.0.1:9", token="t"),
                ),
                patch(
                    "service_handoff.rpc",
                    side_effect=[dict(idle=True, status="stopping"),
                                 dict(admission_frozen=True), ConnectionRefusedError()],
                ) as rpc,
            ):
                self.assertTrue(service_handoff.stop(str(engine)))
                self.assertEqual([c.args[1] for c in rpc.call_args_list],
                                 ["stop_if_idle", "status", "status"])


if __name__ == "__main__":
    unittest.main()
