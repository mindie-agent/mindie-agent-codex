"""A known feed precondition reaches the native caller without private details."""
from contextlib import redirect_stdout
import builtins
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))


class QuerySourceFailureTests(unittest.TestCase):
    def test_missing_transport_keeps_original_safe_failure(self):
        payload = dict(surface="knowledge", name="knowledge_query",
                       arguments={"query": "synthetic"}, mindie_session_id="native-task",
                       native_session_verified=True)
        original_import = builtins.__import__
        original_error = ModuleNotFoundError("private-path secret-canary")

        def missing_transport(name, *args, **kwargs):
            if name == "mindie_knowledge.loop.transport":
                raise original_error
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "codex.json"
            config.write_text(json.dumps(dict(engine_config=str(Path(directory) / "engine.json"))))
            output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
            with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(config)), \
                    patch.object(sys, "stdin", stdin), \
                    patch("builtins.__import__", side_effect=missing_transport), \
                    patch("diagnostic_support.failure", return_value=None) as report, \
                    redirect_stdout(output):
                runpy.run_path(str(SCRIPTS / "runtime_call.py"), run_name="__main__")
            output.flush()
            encoded = output.buffer.getvalue().decode("utf-8")
            result = json.loads(encoded)
            self.assertTrue(result["isError"])
            self.assertNotIn("feed_unavailable", encoded)
            self.assertNotIn("secret-canary", encoded)
            self.assertIn("ModuleNotFoundError", encoded)
            report.assert_called_once()
            self.assertIs(report.call_args.kwargs["exception"], original_error)

    def test_real_helper_main_preserves_typed_failure_without_internal_report(self):
        from mindie_knowledge.loop.transport import FeedUnavailableError
        payload = dict(surface="knowledge", name="knowledge_query",
                       arguments={"query": "synthetic"}, mindie_session_id="native-task",
                       native_session_verified=True)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "codex.json"
            config.write_text(json.dumps(dict(engine_config=str(Path(directory) / "engine.json"))))
            output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
            with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(config)), \
                    patch.object(sys, "stdin", stdin), \
                    patch("service_client.connect", return_value={}), \
                    patch("mindie_knowledge.loop.transport.rpc",
                          side_effect=FeedUnavailableError("private/repository secret-canary")) as rpc, \
                    patch("diagnostic_support.failure", side_effect=AssertionError("misreported internal fault")), \
                    redirect_stdout(output):
                runpy.run_path(str(SCRIPTS / "runtime_call.py"), run_name="__main__")
            output.flush()
            encoded = output.buffer.getvalue().decode("utf-8")
            result = json.loads(encoded)
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["code"], "feed_unavailable")
            self.assertEqual(result["structuredContent"]["stage"], "query-source")
            self.assertEqual(result["structuredContent"]["execution"], "not_started")
            self.assertFalse(result["structuredContent"]["automatic_retry"])
            self.assertNotIn("secret-canary", encoded)
            self.assertNotIn("private/repository", encoded)
            rpc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
