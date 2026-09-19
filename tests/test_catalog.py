"""The generated discovery snapshot must match Gate's strict declared contract."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

from export_catalog import catalog

CATALOG = SCRIPTS / "mcp_catalog.json"


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generated = catalog()
        cls.checked_in = json.loads(CATALOG.read_text())

    def test_snapshot_is_regenerated_from_the_pinned_runtime(self):
        self.assertEqual(
            json.dumps(self.generated, ensure_ascii=False, indent=2) + "\n",
            CATALOG.read_text(),
            "mcp_catalog.json is stale; run export_catalog.py with the pinned interpreter",
        )

    def test_every_tool_fails_closed_on_undeclared_keys(self):
        for surface in ("knowledge", "remote"):
            for tool in self.generated[surface]:
                self.assertIs(
                    tool["inputSchema"].get("additionalProperties"),
                    False,
                    tool["name"],
                )

    def test_custom_resolver_boilerplate_is_not_advertised(self):
        for tool in self.generated["remote"]:
            text = json.dumps(tool)
            self.assertNotIn("consumer-registered resolver", text)

    def test_remote_job_stop_preserves_force(self):
        stop = next(
            t for t in self.generated["remote"] if t["name"] == "remote_job_stop"
        )
        self.assertEqual(stop["inputSchema"]["properties"]["force"]["type"], "boolean")

    def test_remote_write_keeps_rejecting_timeout_keys(self):
        write = next(t for t in self.generated["remote"] if t["name"] == "remote_write")
        self.assertNotIn("timeout_ms", write["inputSchema"]["properties"])
        self.assertNotIn("timeout", write["inputSchema"]["properties"])

    def test_artifact_transfers_advertise_the_call_timeout(self):
        for name in ("remote_artifact_pull", "remote_artifact_push"):
            tool = next(t for t in self.generated["remote"] if t["name"] == name)
            self.assertEqual(
                tool["inputSchema"]["properties"]["timeout_ms"]["type"], "integer"
            )


if __name__ == "__main__":
    unittest.main()
