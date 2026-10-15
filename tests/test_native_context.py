"""Installed native paths are explicit, bounded and contain no provider secrets."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import native_context
from session_gate import generation_env


class NativeContextTests(unittest.TestCase):
    def test_saved_installation_wins_over_wrong_or_missing_host_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            config = base / "adapter.json"
            expected = {"CODEX_HOME": str(base / "selected-home"),
                        "MINDIE_CODEX_BIN": str(base / "selected-bin")}
            config.write_text(json.dumps({"native_context": expected}), encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(base / "wrong-home"),
                                         "MINDIE_CODEX_BIN": str(base / "wrong-bin")}):
                env = generation_env(config)
            self.assertEqual({key: env[key] for key in native_context.KEYS}, expected)
            self.assertEqual(env["MINDIE_AGENT_CONFIG"], str(config))

    def test_invalid_saved_context_does_not_fall_back_to_ambient_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "adapter.json"
            for value in (None, [], {"CODEX_HOME": "relative"},
                          {"MINDIE_CODEX_BIN": False}, {"OPENAI_API_KEY": "private"}):
                with self.subTest(value=value):
                    config.write_text(json.dumps({"native_context": value}), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        generation_env(config)

    def test_capture_contains_only_paths_and_default_home_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "private"}, clear=True), \
                patch.object(native_context.shutil, "which", return_value=None), \
                patch.object(native_context.Path, "home", return_value=Path(temporary)):
            self.assertEqual(native_context.capture(),
                             {"CODEX_HOME": str(Path(temporary) / ".codex")})

    def test_command_name_uses_path_and_missing_selection_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            executable = base / "bin/codex"
            with patch.object(native_context.shutil, "which", return_value=str(executable)) as which:
                value = native_context.capture(home=base, binary="codex")
                self.assertEqual(value["MINDIE_CODEX_BIN"], str(executable))
                which.assert_called_once_with("codex")
            with patch.object(native_context.shutil, "which", return_value=None):
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    native_context.capture(home=base, binary="missing-native")
            with patch.object(native_context.shutil, "which", side_effect=AssertionError("path is explicit")):
                self.assertEqual(native_context.capture(home=base, binary=executable)["MINDIE_CODEX_BIN"],
                                 str(executable))
                relative = base / "relative-codex"
                relative.touch()
                with patch.object(native_context.os, "getcwd", return_value=str(base)):
                    self.assertEqual(native_context.capture(home=base, binary="./relative-codex")["MINDIE_CODEX_BIN"],
                                     str(relative))


if __name__ == "__main__":
    unittest.main()
