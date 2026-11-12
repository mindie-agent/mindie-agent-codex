"""The generated discovery snapshot must match Gate's strict declared contract."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_rebinding_during_discovery_cannot_cache_old_tools_as_new(self):
        import live_catalog
        from mcp_gate import Gate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindings = {}
            for generation in ("old", "new"):
                scripts = root / generation
                scripts.mkdir()
                tool = dict(name="catalog_generation", description=generation, inputSchema=dict(
                    type="object", properties={generation: dict(type="string")},
                    required=[generation], additionalProperties=False))
                (scripts / "mcp_catalog.json").write_text(
                    json.dumps(dict(remote=[tool])), encoding="utf-8", newline="\n")
                bindings[generation] = dict(python=sys.executable, runtime_scripts=str(scripts))
            config = root / "adapter.json"
            config.write_text(json.dumps(bindings["old"]), encoding="utf-8", newline="\n")
            read_text = Path.read_text
            switched = False

            def read_then_publish(path, *args, **kwargs):
                nonlocal switched
                value = read_text(path, *args, **kwargs)
                if path == config and not switched:
                    switched = True
                    replacement = root / "next.json"
                    replacement.write_text(
                        json.dumps(bindings["new"]), encoding="utf-8", newline="\n")
                    replacement.replace(config)
                return value

            with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(config)), \
                    patch.dict(live_catalog._cache, clear=True), \
                    patch.object(Path, "read_text", read_then_publish):
                gate = Gate("remote")
                self.assertEqual(gate.tools[0]["description"], "old")
                selected = gate.tools[0]
                self.assertEqual(selected["description"], "new")
                self.assertEqual(selected["inputSchema"]["required"], ["new"])

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
        self.assertTrue(authentication["password"]["writeOnly"])
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

    def test_native_gate_accepts_business_interpreter_probe_arguments(self):
        from mcp_gate import Gate
        arguments = {
            "host": "example.invalid", "port": 22,
            "distributions": ["vllm-ascend"],
            "python_executable": "/business/venv/bin/python",
            "python_candidates": ["/custom/bin/python"],
            "required_distributions": ["torch", "torch-npu"],
        }
        name, accepted = Gate("remote")._tool_args({"params": {
            "name": "remote_probe", "arguments": arguments,
        }})
        self.assertEqual(name, "remote_probe")
        self.assertEqual(accepted, arguments)

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
