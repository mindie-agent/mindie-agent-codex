"""Startup controller routing, convergence, and native-writer boundaries."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import native_entry
import release_migration
from update_lock import file_lock, update_lock


def target_release(root, version, config):
    release = Path(root) / "release"
    release.mkdir(exist_ok=True)
    validation = dict(native_version=version, status="validated",
                      schema="mindie-candidate-check/1")
    digest = hashlib.sha256(str(Path(config).absolute()).encode()).hexdigest()
    engine = release / ("profile-" + digest + ".engine.json")
    engine.write_text("{}")
    (release / "runtime.json").write_text(json.dumps(
        dict(python=sys.executable, validation=validation)))
    return release, validation, engine


def old_config(config, version):
    value = dict(python=sys.executable, runtime_scripts=str(SCRIPTS),
                 engine_config=str(Path(config).with_name("old.engine.json")),
                 product_validation=dict(native_version=version,
                                          status="validated",
                                          schema="mindie-candidate-check/1"),
                 component_release=None)
    Path(value["engine_config"]).write_text("{}")
    Path(config).write_text(json.dumps(value))
    return value


class LauncherRoutingTests(unittest.TestCase):
    def run_launcher(self, root, code, role):
        env = dict(os.environ, CODEX_HOME=str(Path(root) / "home"),
                   PLUGIN_DATA=str(Path(root) / "data"),
                   PLUGIN_ROOT=str(Path(root) / "plugin"),
                   MINDIE_AGENT_CONFIG=str(Path(root) / "codex.json"),
                   MARK=str(Path(root) / "mark"))
        return subprocess.run([sys.executable, "-c", code, role],
                              capture_output=True, text=True, env=env, timeout=20)

    def test_newer_seed_routes_startup_roles_to_migration_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin/scripts").mkdir(parents=True)
            (root / "plugin/scripts/bootstrap.py").write_text(
                "import json,os,sys;from pathlib import Path;"
                "Path(os.environ['MARK']).write_text(json.dumps(sys.argv[1:]))\n")
            (root / "codex.json").write_text(json.dumps(
                dict(python=sys.executable, product_validation=dict(native_version="0.1.8"))))
            code = native_entry.hook_launcher_code(seed_version="0.1.10")
            result = self.run_launcher(root, code, "tools-mcp")
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads((root / "mark").read_text())
            self.assertEqual(argv[-2:], ["migrate", "tools-mcp"])
            self.assertIn("--source", argv)

    def test_stop_and_user_prompt_keep_the_canonical_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            release = data / "releases/0.1.8"
            release.mkdir(parents=True)
            validation = dict(native_version="0.1.8", status="validated",
                              schema="mindie-candidate-check/1")
            (release / "runtime.json").write_text(json.dumps(
                dict(python=sys.executable, validation=validation)))
            (release / "entry.py").write_text(
                "import json,os,sys;from pathlib import Path;"
                "Path(os.environ['MARK']).write_text(json.dumps(sys.argv[1:]))\n")
            (root / "codex.json").write_text(json.dumps(
                dict(python=sys.executable, product_validation=validation)))
            code = native_entry.hook_launcher_code(seed_version="0.1.10")
            for role in ("stop", "user-prompt-submit", "session-start"):
                with self.subTest(role=role):
                    (root / "mark").unlink(missing_ok=True)
                    result = self.run_launcher(root, code, role)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads((root / "mark").read_text()), [role])

    def test_prepared_fixed_entry_is_used_when_plugin_source_cache_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixed = root / "data/releases/0.1.10"
            fixed.mkdir(parents=True)
            (fixed / "entry.py").write_text(
                "import json,os,sys;from pathlib import Path;"
                "Path(os.environ['MARK']).write_text(json.dumps(sys.argv[1:]))\n")
            (root / "codex.json").write_text(json.dumps(
                dict(python=sys.executable, product_validation=dict(native_version="0.1.8"))))
            code = native_entry.hook_launcher_code(seed_version="0.1.10")
            result = self.run_launcher(root, code, "tools-mcp")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / "mark").read_text()),
                             ["migrate", "tools-mcp"])

    def test_custom_name_and_marketplace_are_used_by_the_migration_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            source = home / "plugins/cache/other-market/other-plugin/0.1.10"
            (source / "scripts").mkdir(parents=True)
            (source / "scripts/bootstrap.py").write_text(
                "import json,os,sys;from pathlib import Path;"
                "Path(os.environ['MARK']).write_text(json.dumps(sys.argv[1:]))\n")
            (root / "codex.json").write_text(json.dumps(
                dict(python=sys.executable, product_validation=dict(native_version="0.1.8"))))
            code = native_entry.hook_launcher_code(
                name="other-plugin", marketplace="other-market", seed_version="0.1.10")
            env = dict(os.environ, CODEX_HOME=str(home),
                       MINDIE_AGENT_CONFIG=str(root / "codex.json"),
                       MARK=str(root / "mark"))
            env.pop("PLUGIN_ROOT", None)
            result = subprocess.run([sys.executable, "-c", code, "tools-mcp"],
                                    capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads((root / "mark").read_text())
            self.assertIn(str(source), argv)

    def test_canonical_newer_than_seed_stays_on_canonical_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            release = data / "releases/0.1.11"
            release.mkdir(parents=True)
            validation = dict(native_version="0.1.11", status="validated",
                              schema="mindie-candidate-check/1")
            (release / "runtime.json").write_text(json.dumps(
                dict(python=sys.executable, validation=validation)))
            (release / "entry.py").write_text(
                "import json,os,sys;from pathlib import Path;"
                "Path(os.environ['MARK']).write_text(json.dumps(sys.argv[1:]))\n")
            (root / "codex.json").write_text(json.dumps(
                dict(python=sys.executable, product_validation=validation)))
            code = native_entry.hook_launcher_code(seed_version="0.1.10")
            result = self.run_launcher(root, code, "tools-mcp")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / "mark").read_text()), ["tools-mcp"])


class MigrationControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "codex.json"
        self.old = old_config(self.config, "0.1.9")
        self.release, self.validation, self.engine = target_release(
            self.root, "0.1.10", self.config)
        self.target = dict(self.old, python=sys.executable,
                           runtime_scripts=str(SCRIPTS),
                           engine_config=str(self.engine),
                           product_validation=self.validation,
                           component_release=None)
        self.env = patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(self.config))
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_busy_old_update_lock_is_deferred_without_changing_canonical(self):
        before = self.config.read_bytes()
        marker = self.root / "held"
        code = (
            "import sys,time;from pathlib import Path;"
            f"sys.path.insert(0,{str(SCRIPTS)!r});"
            "from update_lock import update_lock;"
            f"\nwith update_lock(Path({str(self.config)!r})):"
            f"\n Path({str(marker)!r}).write_text('held');time.sleep(0.8)\n"
        )
        holder = subprocess.Popen([sys.executable, "-I", "-c", code])
        try:
            until = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            result = release_migration.run(
                self.release, "tools-mcp", [],
                deadline=time.monotonic() + 0.2)
            self.assertEqual(result["status"], "deferred")
            self.assertEqual(result["reason"], "startup_deadline")
            self.assertEqual(self.config.read_bytes(), before)
        finally:
            holder.wait(timeout=10)

    def test_concurrent_starter_converges_on_published_target_without_holding_locks(self):
        updates = self.root / "codex.updates"
        updates.mkdir()
        ready = self.root / "leader.ready"
        target = self.target
        code = (
            "import json,time,sys;from pathlib import Path;"
            f"sys.path.insert(0,{str(SCRIPTS)!r});"
            "from update_lock import file_lock;"
            f"\nwith file_lock(Path({str(updates / 'check.lock')!r}), exclusive=True):"
            f"\n Path({str(ready)!r}).write_text('ready');time.sleep(.6)"
            f"\n temporary=Path({str(self.config)!r}+'.tmp')"
            f"\n temporary.write_text(json.dumps({target!r}));temporary.replace({str(self.config)!r})\n"
        )
        leader = subprocess.Popen([sys.executable, "-I", "-c", code])
        try:
            until = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            started = time.monotonic()
            result = release_migration.run(self.release, "tools-mcp", [])
            elapsed = time.monotonic() - started
            self.assertEqual(result["status"], "ready")
            self.assertLess(elapsed, 5)
            with file_lock(updates / "check.lock", exclusive=True):
                pass
            with update_lock(self.config, exclusive=True):
                pass
        finally:
            leader.wait(timeout=10)

    def test_exclusive_wait_covers_a_real_call_drain_within_deadline(self):
        marker = self.root / "drain-held"
        code = (
            "import sys,time;from pathlib import Path;"
            f"sys.path.insert(0,{str(SCRIPTS)!r});"
            "from update_lock import update_lock;"
            f"\nwith update_lock(Path({str(self.config)!r})):"
            f"\n Path({str(marker)!r}).write_text('held');time.sleep(.5)\n"
        )
        holder = subprocess.Popen([sys.executable, "-I", "-c", code])
        try:
            until = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            started = time.monotonic()
            self.assertTrue(release_migration._exclusive_probe(
                self.config, time.monotonic() + 2.0))
            self.assertGreaterEqual(time.monotonic() - started, 0.35)
        finally:
            holder.wait(timeout=10)

    def test_deferred_activation_retries_in_the_same_startup(self):
        calls = []

        def activate(config, prepared, *, control_seconds):
            calls.append(control_seconds)
            if len(calls) == 1:
                return dict(status="deferred", reason="operation_busy",
                            execution="not_started")
            return dict(status="active")

        with patch.object(release_migration, "prepare",
                          return_value=dict(adapter=self.target,
                                            expected_binding=self.old)) as prepare, \
                patch.object(release_migration, "activate", side_effect=activate), \
                patch.object(release_migration, "binding_matches_release",
                             side_effect=lambda *args, **kwargs: len(calls) >= 2):
            result = release_migration._migrate_locked(
                self.config, self.release,
                {"validation": {"native_version": "0.1.10"}},
                time.monotonic() + 2.0)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(calls), 2)
        prepare.assert_called_once()

    def test_unsettled_native_admin_writer_is_visible_and_not_activated(self):
        (self.root / "codex.updates").mkdir(exist_ok=True)
        pending = dict(stage="plugin_add",
                       process=dict(execution="unknown", returncode=None,
                                    cleanup_failed=False))
        (self.root / "codex.updates/native.json").write_text(json.dumps(pending))
        before = self.config.read_bytes()
        result = release_migration.run(self.release, "tools-mcp", [])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "native_writer")
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
