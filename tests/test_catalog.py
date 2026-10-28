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

    def test_remote_catalog_binds_the_codex_profile_native_tools(self):
        from remote_dev.mcp.schemas import ALIASES
        names = [tool["name"] for tool in self.generated["remote"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(set(ALIASES) <= set(names))
        self.assertTrue(
            {"remote_exec_command", "remote_write_stdin", "remote_view_image"}
            <= set(names)
        )
        authentication = next(
            tool for tool in self.generated["remote"]
            if tool["name"] == "remote_auth"
        )["inputSchema"]["properties"]
        self.assertIn("password_env", authentication)
        self.assertNotIn("password", authentication)
        apply_patch = next(
            tool for tool in self.generated["remote"]
            if tool["name"] == "remote_apply_patch"
        )
        self.assertEqual(apply_patch["inputSchema"]["required"], ["patch"])
        self.assertEqual(
            apply_patch["inputSchema"]["properties"]["patch"]["type"], "string"
        )
        write_stdin = next(
            tool for tool in self.generated["remote"]
            if tool["name"] == "remote_write_stdin"
        )
        self.assertEqual(write_stdin["inputSchema"]["required"], ["session_id"])
        self.assertIn(
            "session_id", write_stdin["inputSchema"]["properties"]
        )
        self.assertNotIn("restored from job_id", write_stdin["description"])

    def test_gate_accepts_every_advertised_name_and_rejects_bad_keys(self):
        from mcp_gate import Gate, InvalidToolArguments
        gate = Gate("remote")
        for tool in self.generated["remote"]:
            args = (
                {"patch": "*** Begin Patch\n*** End Patch"}
                if tool["name"] == "remote_apply_patch" else {}
            )
            try:
                gate._tool_args(
                    {"params": {"name": tool["name"], "arguments": args}}
                )
            except InvalidToolArguments as exc:
                self.assertNotIn("Unknown MindIE tool", str(exc), tool["name"])
                self.assertIn("missing keys", str(exc), tool["name"])
        with self.assertRaisesRegex(InvalidToolArguments, "unknown keys"):
            gate._tool_args({"params": {
                "name": "remote_apply_patch",
                "arguments": {
                    "patch": "*** Begin Patch\n*** End Patch",
                    "undeclared": True,
                },
            }})
        with self.assertRaisesRegex(InvalidToolArguments, "missing keys"):
            gate._tool_args({"params": {
                "name": "remote_apply_patch", "arguments": {},
            }})

    def test_remote_endpoint_contract_is_model_visible(self):
        for name in ("remote_bash", "remote_exec_command"):
            tool = next(
                item for item in self.generated["remote"]
                if item["name"] == name
            )
            self.assertIn("host and port together", tool["description"])
            self.assertIn("22", tool["description"])
            self.assertIn("no implicit default", tool["description"].lower())
            host = tool["inputSchema"]["properties"]["host"]["description"]
            port = tool["inputSchema"]["properties"]["port"]["description"]
            self.assertIn("host and port", host)
            self.assertIn("22", port)
            self.assertIn("no implicit default", port.lower())
            self.assertNotIn("host", tool["inputSchema"]["required"])
            self.assertNotIn("port", tool["inputSchema"]["required"])

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
