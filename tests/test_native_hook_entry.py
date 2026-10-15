"""Native Hook commands retain identity while the installed binding changes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import native_entry


class NativeHookEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.config = self.root / "codex.json"
        self.source = self.root / "evicted-native-cache"
        self.environment = dict(
            PLUGIN_DATA=str(self.data), PLUGIN_ROOT=str(self.source),
            CODEX_HOME=str(self.root / "native"), MINDIE_AGENT_CONFIG=str(self.config))

    def prepared(self, version):
        release = self.data / "releases" / version
        release.mkdir(parents=True)
        validation = dict(native_version=version, schema="mindie-candidate-check/1",
                          status="validated", source_sha256=version)
        adapter = dict(python=sys.executable, product_validation=validation)
        self.config.write_text(json.dumps(adapter))
        (release / "runtime.json").write_text(json.dumps(
            dict(python=sys.executable, validation=validation)))
        entry = release / "entry.py"
        entry.write_text("# immutable installed fixture entry\n")
        return entry

    def launch(self, role):
        calls = []
        with patch.dict(os.environ, self.environment), \
                patch.object(sys, "argv", ["loader", role]), \
                patch.object(sys, "path", list(sys.path)), \
                patch("runpy.run_path", side_effect=lambda path, **kw: calls.append(
                    (path, list(sys.argv), kw))):
            exec(native_entry.hook_launcher_code(), {})
        return calls

    def test_hook_commands_do_not_change_with_release_but_mcp_keeps_fixed_entry(self):
        (self.source / ".codex-plugin").mkdir(parents=True)
        (self.source / "hooks").mkdir()
        manifests = []
        for version in ("0.0.1", "0.0.2"):
            (self.source / ".codex-plugin/plugin.json").write_text(json.dumps(dict(version=version)))
            native_entry.write_manifests(self.source)
            manifests.append(((self.source / "hooks/hooks.json").read_bytes(),
                              (self.source / ".mcp.json").read_bytes()))
        self.assertEqual(manifests[0][0], manifests[1][0])
        self.assertNotEqual(manifests[0][1], manifests[1][1])
        self.assertIn("--offline", native_entry.hook_commands("stop")["command"])
        self.assertIn("--no-python-downloads", native_entry.hook_commands("stop")["command"])

    def test_same_loaded_command_uses_exact_active_prepared_entry_without_native_cache(self):
        old = self.prepared("0.0.1")
        self.assertEqual(self.launch("stop")[0][0], str(old))
        new = self.prepared("0.0.2")
        self.assertFalse(self.source.exists())
        for role in ("session-start", "stop"):
            calls = self.launch(role)
            self.assertEqual(calls, [(str(new), [str(new), role], {"run_name": "__main__"})])
        self.assertTrue(old.is_file())

    def test_first_session_uses_host_source_but_stop_never_bootstraps(self):
        bootstrap = self.source / "scripts/bootstrap.py"
        bootstrap.parent.mkdir(parents=True)
        bootstrap.write_text("# fixture\n")
        self.assertEqual(self.launch("session-start"), [(
            str(bootstrap),
            [str(bootstrap), str(self.data), "--source", str(self.source), "session-start"],
            {"run_name": "__main__"})])
        with self.assertRaisesRegex(RuntimeError, "not prepared"):
            self.launch("stop")
        self.assertFalse(self.config.exists())
        self.assertFalse(self.data.exists())

    def test_missing_or_mismatched_prepared_entry_never_falls_back_to_source(self):
        entry = self.prepared("0.0.1")
        entry.unlink()
        with self.assertRaisesRegex(RuntimeError, "entry is unavailable"):
            self.launch("session-start")
        entry.write_text("# fixture\n")
        receipt = entry.with_name("runtime.json")
        value = json.loads(receipt.read_text())
        value["python"] = "/other/runtime"
        receipt.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "runtime receipt differs"):
            self.launch("session-start")
        self.assertFalse(self.source.exists())

    def test_invalid_metadata_is_never_first_install(self):
        for contents in ("{", "[]", '{"x":"' + "x" * 65536 + '"}'):
            with self.subTest(contents=contents[:12]):
                self.config.write_text(contents)
                with self.assertRaises(ValueError):
                    self.launch("session-start")
                self.assertFalse(self.data.exists())
        self.config.unlink()
        self.config.mkdir()
        with self.assertRaises((ValueError, OSError)):
            self.launch("session-start")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_fifo_profile_does_not_block_or_bootstrap(self):
        os.mkfifo(self.config)
        with self.assertRaisesRegex(ValueError, "must be regular"):
            self.launch("session-start")
        self.assertFalse(self.data.exists())

    @unittest.skipUnless(os.name == "posix", "unprivileged POSIX symlink")
    def test_symlink_profile_and_entry_are_not_executed(self):
        target = self.prepared("0.0.1")
        original = self.config.with_suffix(".original")
        self.config.rename(original)
        self.config.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "must be regular"):
            self.launch("stop")
        self.config.unlink()
        original.rename(self.config)
        target.unlink()
        target.symlink_to(original)
        with self.assertRaisesRegex(RuntimeError, "entry is unavailable"):
            self.launch("stop")


if __name__ == "__main__":
    unittest.main()
