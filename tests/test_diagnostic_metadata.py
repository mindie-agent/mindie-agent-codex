"""Read version evidence from actual old-format packages without modifying them."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts/diagnostic_support.py"


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.revision = "a" * 40
        self.plugin = Path(self.tmp.name) / self.revision / "plugin"
        scripts = self.plugin / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(SOURCE, scripts / SOURCE.name)
        (self.plugin / ".codex-plugin").mkdir()
        self.version = "0.1.0+codex.20260922124146470691"
        self.write(self.plugin / ".codex-plugin/plugin.json",
                   {"name": "mindie-agent", "version": self.version})
        self.receipt = {"plugin": str(self.plugin), "revision": self.revision,
                        "version": self.version}
        self.write(self.plugin.parent / "prepared.json", self.receipt)
        spec = importlib.util.spec_from_file_location("metadata_candidate", scripts / SOURCE.name)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data))

    def test_previous_updater_package_has_exact_identity_without_new_stamp(self):
        self.assertEqual(self.module.build_metadata(),
                         {"version": self.version, "revision": self.revision})
        self.assertFalse((self.plugin / "scripts/diagnostic-build.json").exists())

    def test_receipt_for_another_package_cannot_supply_revision(self):
        self.receipt["plugin"] = str(self.plugin.parent / "another-plugin")
        self.write(self.plugin.parent / "prepared.json", self.receipt)
        self.assertEqual(self.module.build_metadata(), {"version": self.version})

    def test_native_cache_manifest_needs_no_generation_receipt(self):
        (self.plugin.parent / "prepared.json").unlink()
        self.assertEqual(self.module.build_metadata(), {"version": self.version})

    def test_symlink_stamp_is_not_read(self):
        target = Path(self.tmp.name) / "unrelated.json"
        self.write(target, {"revision": "b" * 40, "version": "9.9.9"})
        (self.plugin / "scripts/diagnostic-build.json").symlink_to(target)
        self.assertEqual(self.module.build_metadata(),
                         {"version": self.version, "revision": self.revision})
