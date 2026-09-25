"""Schema and knowledge-page wire. No native host turn metadata is attached.

Identity is the adapter resolve_lease seam: a missing or mismatched session
fails before a service starts. The page is a local Store.explain fixture from the installed
mindie_knowledge dependency, returned through runtime_call's real JSON shape,
including next_offset.
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

PAGE = 32768
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
    if args.get("limit") != 32768:
        raise AssertionError(args.get("limit"))
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
    print(json.dumps(runtime_call.call(payload), ensure_ascii=False))
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
    extra = 8
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

    def test_explain_schema_maximum_and_query_unchanged(self):
        from export_catalog import catalog

        generated = catalog()
        checked = json.loads((SCRIPTS / "mcp_catalog.json").read_text())
        explain = next(item for item in generated["knowledge"] if item["name"] == "knowledge_explain")
        query = next(item for item in generated["knowledge"] if item["name"] == "knowledge_query")
        self.assertEqual(explain["inputSchema"]["properties"]["limit"]["maximum"], PAGE)
        self.assertEqual(explain["inputSchema"]["required"], ["ref"])
        self.assertIs(explain["inputSchema"]["additionalProperties"], False)
        self.assertIn("next_offset", explain["description"])
        self.assertIn("slice", explain["description"])
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

    def test_store_explain_page_fits_knowledge_bound(self):
        from mindie_knowledge.loop.store import Store

        knowledge_bound = mcp_gate.KNOWLEDGE_MAX_OUTPUT

        store = Store(self.tmp / "store", "vllm-ascend")
        sizes = {}
        nonbmp_out = None
        observed = {
            "explain_page_chars": getattr(Store, "EXPLAIN_PAGE_CHARS", None),
            "explain_max_limit": getattr(Store, "EXPLAIN_MAX_LIMIT", None),
        }
        try:
            for name, body in _bodies().items():
                self.assertEqual(len(body), PAGE + 8)
                doc = store.create_draft(
                    kind="knowledge",
                    title="Local page fixture",
                    summary="Local component fixture only.",
                    content=body,
                )
                ref = store.ref(doc["entry_id"], doc["revision"])
                offset = 0
                for _ in range(3):
                    page = store.explain(ref, offset=offset, limit=PAGE)
                    path = self.tmp / f"{name}-{offset}.json"
                    path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
                    out = self._run("page", ref, offset, path, session=SESSION)
                    parsed = json.loads(out)
                    text = json.loads(parsed["content"][0]["text"])
                    self.assertIs(parsed["isError"], False)
                    self.assertEqual(text, page)
                    self.assertEqual(parsed["structuredContent"], page)
                    self.assertEqual(parsed["structuredContent"]["ref"], ref)
                    nbytes = len(out.encode())
                    self.assertLessEqual(nbytes, knowledge_bound, f"{name} {nbytes}")
                    self.assertIn("next_offset", parsed["structuredContent"])
                    self.assertEqual(parsed["structuredContent"]["next_offset"], page["next_offset"])
                    if offset == 0:
                        self.assertEqual(len(text["content"]), PAGE)
                        self.assertEqual(text["content"], body[:PAGE])
                        sizes[name] = nbytes
                        if name == "nonbmp":
                            nonbmp_out = out
                    else:
                        self.assertEqual(text["content"], body[offset:])
                    nxt = page["next_offset"]
                    if nxt is None:
                        self.assertEqual(offset + len(page["content"]), page["content_length"])
                        break
                    self.assertIsInstance(nxt, int)
                    self.assertEqual(nxt, offset + len(page["content"]))
                    offset = nxt
                else:
                    self.fail("paging did not reach the end")
        finally:
            store.close()
        self.assertGreater(sizes["nonbmp"], 256 * 1024)
        with self.assertRaises(ValueError):
            bounded_process.run(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                nonbmp_out,
                timeout=10,
                max_output=256 * 1024,
            )
        observed["bytes"] = sizes
        observed["bound"] = knowledge_bound
        print("KNOWLEDGE_PAGE_BYTES " + json.dumps(observed, sort_keys=True))

    def _run(self, mode, ref, offset, page, *, session):
        payload = {
            "surface": "knowledge",
            "name": "knowledge_explain",
            "arguments": {"ref": ref, "offset": offset, "limit": PAGE},
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
