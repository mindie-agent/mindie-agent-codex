"""Schema and knowledge-page wire. No native host turn metadata is attached.

Identity is the adapter resolve_lease seam: a missing or mismatched session
fails before a service starts. The page is a local Store.explain fixture from the installed
mindie_knowledge dependency, returned through runtime_call's real JSON shape,
including adjacent block references and current task navigation.
"""

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

import bounded_process
import mcp_gate

PAGE = 4096
SESSION = "component-session"

HELPER = r"""
import json, os, sys
sys.path.insert(0, os.environ["SCRIPTS"])
os.environ["MINDIE_AGENT_CONFIG"] = os.environ["CONFIG"]
import runtime_call
import mindie_knowledge.loop.cli as cli
import mindie_knowledge.loop.transport as transport

page = json.loads(open(os.environ["PAGE"], encoding="utf-8").read())
mode = os.environ["MODE"]
started = {"service": False}

def ensure_service(*args, **kwargs):
    started["service"] = True
    if mode != "page":
        raise AssertionError("knowledge service started without a matching session")
    return object()

def rpc(connection, method, args, timeout=5):
    if method != "explain":
        raise AssertionError(method)
    if args.get("ref") != os.environ["REF"]:
        raise AssertionError("ref")
    if "offset" in args or "limit" in args:
        raise AssertionError("retired paging arguments reached runtime")
    if args.get("_session_id") != os.environ["SESSION"] and mode == "page":
        raise AssertionError("session was not the gate-bound id")
    return page

cli.ensure_service = ensure_service
transport.rpc = rpc
if mode == "page":
    runtime_call.resolve_lease = lambda config, token: {"session": os.environ["SESSION"]}
    runtime_call.finish_outcome = lambda *args, **kwargs: None
elif mode == "mismatch":
    runtime_call.resolve_lease = lambda config, token: {"session": os.environ["SESSION"]}
    runtime_call.finish_outcome = lambda *args, **kwargs: None

payload = json.loads(sys.stdin.read())
if mode == "page":
    # Match runtime_call's UTF-8 wire, independent of the fixture's console
    # encoding. The production CLI writes bytes too.
    sys.stdout.buffer.write((json.dumps(runtime_call.call(payload), ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
else:
    try:
        runtime_call.call(payload)
    except Exception as exc:
        print(json.dumps({
            "seam": type(exc).__name__,
            "message": str(exc)[:300],
            "service_started": started["service"],
        }))
    else:
        print(json.dumps({"seam": "unexpected-success", "service_started": started["service"]}))
"""


def _bodies():
    extra = 0
    escape = ('\\"' * ((PAGE + extra) // 2 + 1))[: PAGE + extra]
    return {
        "ascii": "A" * (PAGE + extra),
        "chinese": "\u6d4b" * (PAGE + extra),
        "nonbmp": "\U00020000" * (PAGE + extra),
        "escape": escape,
    }


class KnowledgePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = self.tmp / "codex.json"
        self.engine = self.tmp / "engine.json"
        self.admission = self.tmp / "admission.sqlite3"
        self.engine.write_text(json.dumps({
            "root": str(self.tmp / "data"),
            "domain": "vllm-ascend",
            "admission_path": str(self.admission),
        }))
        self.config.write_text(json.dumps({
            "python": sys.executable,
            "engine_config": str(self.engine),
            "admission_path": str(self.admission),
            "runtime_scripts": str(SCRIPTS),
        }))
        self.diag = self.tmp / "diag"
        (self.diag / "logs").mkdir(parents=True)
        (self.diag / "off.json").write_text('{"decision":"disabled"}\n')
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "MINDIE_AGENT_CONFIG",
                "MINDIE_REMOTE_STATE_DIR",
                "MINDIE_DIAGNOSTICS_CONFIG",
                "MINDIE_DIAGNOSTICS_ROOT",
            )
        }
        os.environ["MINDIE_AGENT_CONFIG"] = str(self.config)
        os.environ["MINDIE_REMOTE_STATE_DIR"] = str(self.tmp / "remote")
        os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(self.diag / "off.json")
        os.environ["MINDIE_DIAGNOSTICS_ROOT"] = str(self.diag / "logs")
        helper = self.tmp / "helper.py"
        helper.write_text(HELPER)
        self.helper = helper

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_explain_schema_ref_only_and_query_continuation(self):
        from export_catalog import catalog

        generated = catalog()
        checked = json.loads((SCRIPTS / "mcp_catalog.json").read_text())
        explain = next(item for item in generated["knowledge"] if item["name"] == "knowledge_explain")
        query = next(item for item in generated["knowledge"] if item["name"] == "knowledge_query")
        self.assertEqual(set(explain["inputSchema"]["properties"]), {"ref"})
        self.assertEqual(explain["inputSchema"]["required"], ["ref"])
        self.assertIs(explain["inputSchema"]["additionalProperties"], False)
        self.assertIn("block", explain["description"])
        self.assertIn("continuation", query["inputSchema"]["properties"])
        self.assertEqual(query["inputSchema"]["required"], [])
        self.assertNotIn("offset", query["inputSchema"]["properties"])
        self.assertEqual(query["inputSchema"]["properties"]["limit"]["maximum"], 20)
        self.assertEqual(
            json.dumps(generated, ensure_ascii=False, indent=2) + "\n",
            (SCRIPTS / "mcp_catalog.json").read_text(),
        )
        self.assertEqual(checked["knowledge"][1]["name"], "knowledge_explain")

    def test_identity_seam_fails_before_the_service(self):
        page = self.tmp / "empty.json"
        page.write_text("{}", encoding="utf-8")
        missing = json.loads(self._run("unbound", "mindie://vllm-ascend/none", 0, page, session="nobody"))
        self.assertEqual(missing["seam"], "ValueError")
        self.assertIn("activation", missing["message"])
        self.assertIs(missing["service_started"], False)
        mismatched = json.loads(self._run("mismatch", "mindie://vllm-ascend/none", 0, page, session="other-session"))
        self.assertEqual(mismatched["seam"], "ValueError")
        self.assertIn("identity mismatch", mismatched["message"])
        self.assertIs(mismatched["service_started"], False)

    def test_store_single_block_and_navigation_fit_knowledge_bound(self):
        import hashlib
        from mindie_knowledge.loop.store import Store
        from mindie_knowledge.materials import MaterialStore
        from mindie_knowledge.materials.references import task_ref

        store = Store(self.tmp / "store", "vllm-ascend")
        author = MaterialStore(self.tmp / "author", "vllm-ascend")
        self.addCleanup(store.close)
        self.addCleanup(author.close)
        for name, body in _bodies().items():
            task = hashlib.sha256(name.encode()).hexdigest()
            author.append_batch(task, [dict(block_id="a" * 64, text=body,
                                source_range={"part": 1}, title="Local block", summary="Local fixture")],
                                "Local wire fixture", title="Local fixture", status="complete", promote=True)
            store.install_feed([author.export_task(task)], feed_ident="f" * 64)
            navigation = store.explain(task_ref("vllm-ascend", task))
            self.assertNotIn("content", navigation)
            self.assertEqual(navigation["block_count"], 1)
            for result in (navigation, store.explain(navigation["first_block_ref"])):
                path = self.tmp / (name + result["kind"] + ".json")
                path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                output = self._run("page", result["ref"], 0, path, session=SESSION)
                parsed = json.loads(output)
                self.assertIs(parsed["isError"], False)
                self.assertEqual(parsed["structuredContent"], result)
                self.assertEqual(json.loads(parsed["content"][0]["text"]), result)
                self.assertLessEqual(len(output.encode()), mcp_gate.KNOWLEDGE_MAX_OUTPUT)
                self.assertNotIn("next_offset", result)
                self.assertNotEqual(result["ref"], result["feedback_ref"])
                if result["kind"] == "block":
                    self.assertEqual(result["content"], body)
                    self.assertIsNone(result["next_block_ref"])

    def _run(self, mode, ref, offset, page, *, session):
        payload = {
            "surface": "knowledge",
            "name": "knowledge_explain",
            "arguments": {"ref": ref},
            "mindie_session_id": session,
            "mindie_activation": "component-token",
        }
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.update({
            "SCRIPTS": str(SCRIPTS),
            "CONFIG": str(self.config),
            "PAGE": str(page),
            "REF": ref,
            "OFFSET": str(offset),
            "SESSION": SESSION,
            "MODE": mode,
            "MINDIE_AGENT_CONFIG": str(self.config),
            "MINDIE_DIAGNOSTICS_CONFIG": os.environ["MINDIE_DIAGNOSTICS_CONFIG"],
            "MINDIE_DIAGNOSTICS_ROOT": os.environ["MINDIE_DIAGNOSTICS_ROOT"],
        })
        return bounded_process.run(
            [sys.executable, str(self.helper)],
            json.dumps(payload),
            timeout=30,
            max_output=mcp_gate.KNOWLEDGE_MAX_OUTPUT,
            env=env,
        )

    def test_knowledge_helper_bound_is_not_applied_to_remote(self):
        seen = []

        def fake_run(command, data, **kwargs):
            seen.append(kwargs)
            return json.dumps({
                "content": [{"type": "text", "text": "{}"}],
                "structuredContent": {"outcome": "success"},
                "isError": False,
            })

        knowledge = mcp_gate.Gate("knowledge")
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "knowledge_explain", "arguments": {"ref": "mindie://vllm-ascend/x"}},
        }
        with (
            patch.object(knowledge.sessions, "check", return_value={"token": "tok"}),
            patch.object(knowledge.sessions, "claim", return_value=True),
            patch("mcp_gate.run", fake_run),
        ):
            result = knowledge._call(request, SESSION)
        self.assertIs(result["isError"], False)
        self.assertEqual(seen[-1]["max_output"], mcp_gate.KNOWLEDGE_MAX_OUTPUT)

        remote = mcp_gate.Gate("remote")
        remote_request = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "remote_job_status", "arguments": {"job_id": "job-1234"}},
        }
        with patch("mcp_gate.run", fake_run):
            remote_result = remote._remote_call(remote_request, None, None, (SESSION, "unused"))
        self.assertIs(remote_result["isError"], False)
        self.assertNotIn("max_output", seen[-1])

    def test_knowledge_bound_accepts_output_past_512kib(self):
        self.assertEqual(mcp_gate.KNOWLEDGE_MAX_OUTPUT, 1024 * 1024)
        payload = "x" * (512 * 1024 + 1)
        bounded_process.run(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            payload,
            timeout=10,
            max_output=mcp_gate.KNOWLEDGE_MAX_OUTPUT,
        )
        with self.assertRaises(ValueError):
            bounded_process.run(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                payload,
                timeout=10,
                max_output=512 * 1024,
            )


if __name__ == "__main__":
    unittest.main()
