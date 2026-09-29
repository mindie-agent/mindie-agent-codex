"""A real MCP pipe must use UTF-8 independently of the console's encoding."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


class ProtocolEncodingTests(unittest.TestCase):
    def test_non_ascii_request_id_round_trips_under_cp1252(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp1252",
                       MINDIE_AGENT_CONFIG=str(Path(temp) / "missing.json"))
            ident = "\u6d4b\U00020000"
            request = {"jsonrpc": "2.0", "id": ident, "method": "ping"}
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "bridge.py"), "mcp"],
                input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
                capture_output=True, env=env, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"jsonrpc": "2.0", "id": ident, "result": {}})


if __name__ == "__main__":
    unittest.main()
