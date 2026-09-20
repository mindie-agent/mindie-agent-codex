"""Exercise two real Git revisions without network, models or a real Codex install."""

import json
import io
import os
from pathlib import Path
import shutil
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
from session_gate import Sessions
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
                cache = (
                    Path(self.settings["codex_home"])
                    / "plugins/cache/mindie-agent/mindie-agent"
                )
                shutil.rmtree(cache)
                cache.mkdir(parents=True)
                if self.fail_install:
                    self.fail_install = False  # Rollback CLI succeeds.
                    raise RuntimeError("fixture installation failed")
            return "{}"
        if args[0] == "fixture-uv":
            if args[1] == "venv":
                runtime = Path(args[-1]) / "bin/python"
                runtime.parent.mkdir(parents=True)
                runtime.touch()
                self.builds += 1
            return ""
        if len(args) > 1 and args[1].endswith("update_idle.py"):
            return json.dumps(dict(idle=self.idle))
        return super().command(args, **kwargs)

    def probe_runtime(self, python):
        self.assert_runtime = Path(python).exists()
        if not self.assert_runtime:
            raise RuntimeError("runtime missing")

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
        shutil.copy(ROOT / "domain-requirements.txt", self.remote)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.sha = self.commit("first")
        self.root = self.base / "updates"
        self.root.mkdir()
        self.config = self.base / "adapter.json"
        self.engine = self.base / "engine.json"
        atomic(
            self.engine,
            dict(
                root=str(self.base / "data"),
                domain="test",
                agent_command=["old-worker"],
            ),
        )
        atomic(self.config, dict(python=sys.executable, engine_config=str(self.engine)))
        self.initial = read(self.config)
        self.settings = self.base / "updater.json"
        self.cache = (
            self.base / "codex/plugins/cache/mindie-agent/mindie-agent/old/scripts"
        )
        self.cache.mkdir(parents=True)
        (self.cache / "bridge.py").write_text("retained safe entrypoint")
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

    def tearDown(self):
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

    def test_missing_contract_never_replaces_local_safety_fix(self):
        (self.remote / "update-contract.json").unlink()
        sha = self.commit("old unsafe main")
        for _ in range(5):
            result = self.check()
            self.assertEqual(result["status"], "waiting_for_compatible_source")
        self.assertEqual(result["attempts"][sha]["count"], 1)
        self.assertEqual(read(self.config), self.initial)
        self.assertEqual(self.updater.installs, 0)

    def test_active_session_defers_prepared_update_without_consuming_retries(self):
        with patch.dict(os.environ, CODEX_THREAD_ID="fixture-manual"):
            sessions = Sessions(self.config)
            lease = sessions.activate()
            for _ in range(4):
                result = self.check()
                self.assertEqual(result["status"], "waiting_for_idle")
                sessions.check(lease["mindie_session_id"], lease["mindie_activation"])
            self.assertEqual(self.updater.builds, 1)
            self.assertEqual(result["attempts"][self.sha]["count"], 0)
            sessions.deactivate()
        self.assertEqual(self.check()["status"], "installed")

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

    def test_unreadable_lease_state_defers_switch_and_preserves_store(self):
        sessions = Sessions(self.config)
        sessions.path.write_text("not a sqlite database")
        for _ in range(2):
            result = self.check()
            self.assertEqual(result["status"], "waiting_for_idle")
            self.assertIn("lease state unreadable", result["error"])
        self.assertEqual(sessions.path.read_text(), "not a sqlite database")
        self.assertEqual(self.updater.installs, 0)
        self.assertEqual(read(self.config), self.initial)

    def test_dependency_overlay_records_exact_revision_and_dirty_state(self):
        overlay = self.base / "overlay-core"
        overlay.mkdir()
        (overlay / "pyproject.toml").write_text("[project]\nname='overlay'\n")
        subprocess.run(["git", "-C", overlay, "init", "-q"], check=True)
        subprocess.run(["git", "-C", overlay, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", overlay, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "overlay"],
            check=True,
        )
        revision = subprocess.check_output(
            ["git", "-C", overlay, "rev-parse", "HEAD"], text=True
        ).strip()
        (overlay / "local.txt").write_text("uncommitted integration change")
        settings = read(self.settings)
        settings["dependency_overlay"] = [str(overlay)]
        atomic(self.settings, settings)
        self.updater = LocalUpdater(self.settings)
        result = self.check()
        self.assertEqual(result["status"], "installed")
        evidence = result["current"]["dependency_overlay"]
        self.assertEqual(evidence[0]["revision"], revision)
        self.assertIs(evidence[0]["dirty"], True)
        self.assertEqual(evidence[0]["path"], str(overlay))

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


if __name__ == "__main__":
    unittest.main()
