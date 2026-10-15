"""Explicit diagnostic paths survive native environment filtering and updates."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import diagnostic_support


class DiagnosticContextTests(unittest.TestCase):
    def test_only_explicit_paths_are_captured_and_missing_context_preserves_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            selected = {
                "MINDIE_DIAGNOSTICS_CONFIG": str(base / "policy.json"),
                "MINDIE_DIAGNOSTICS_ROOT": str(base / "events"),
            }
            source = dict(selected, GITHUB_TOKEN="never-copy", OTHER_CONFIG="unrelated")
            self.assertEqual(diagnostic_support.capture_environment_context(source), selected)
            self.assertEqual(diagnostic_support.capture_environment_context({}), {})
            untouched = dict(source)
            diagnostic_support.apply_environment_context({}, untouched)
            self.assertEqual(untouched, source)
            self.assertFalse((base / "policy.json").exists())
            self.assertFalse((base / "events").exists())

    def test_invalid_saved_context_is_not_treated_as_the_shared_default(self):
        for value in (None, [], {"GITHUB_TOKEN": "secret"},
                      {"MINDIE_DIAGNOSTICS_ROOT": "relative"},
                      {"MINDIE_DIAGNOSTICS_CONFIG": False}):
            with self.subTest(context=value):
                environment = {"unchanged": "sentinel"}
                with self.assertRaises(ValueError):
                    diagnostic_support.apply_environment_context(
                        {"diagnostics_context": value}, environment)
                self.assertEqual(environment, {"unchanged": "sentinel"})

    def test_context_document_is_bounded_and_invalid_bytes_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "adapter.json"
            self.assertEqual(diagnostic_support.read_environment_context(config), {})
            for content in ("{", "null", " " * 65537):
                config.write_text(content)
                with self.assertRaises(ValueError):
                    diagnostic_support.read_environment_context(config)
            config.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(config)
                with self.assertRaises(ValueError):
                    diagnostic_support.read_environment_context(config)

    def test_stable_native_roles_and_helpers_restore_context_after_generation_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "updates"
            root.mkdir()
            config = base / "adapter.json"
            selected = {
                "MINDIE_DIAGNOSTICS_CONFIG": str(base / "policy.json"),
                "MINDIE_DIAGNOSTICS_ROOT": str(base / "events"),
            }
            native = {"CODEX_HOME": str(base / "native-home"),
                      "MINDIE_CODEX_BIN": str(base / "native-codex")}
            expected = dict(selected, **native)
            native_home = Path(native["CODEX_HOME"])
            native_home.mkdir()
            catalog = native_home / "models.json"
            catalog.write_text(json.dumps({"models": [{"slug": "native-route/gpt-5.6-luna"}]}),
                               encoding="utf-8")
            (native_home / "config.toml").write_text(
                'model_provider="selected"\nmodel_catalog_json=' + json.dumps(str(catalog))
                + '\n[model_providers.selected]\nname="Fixture"\nbase_url="http://127.0.0.1:1/v1"\n',
                encoding="utf-8")
            for name in ("runtime_launcher.py", "update_lock.py", "diagnostic_support.py"):
                shutil.copy2(SCRIPTS / name, root / name)
            (root / "diagnostic_support.py").write_text(
                "raise RuntimeError('stable diagnostic metadata must not load on success')\n")
            probe = (
                "import json,os,subprocess,sys\n"
                "from pathlib import Path\n"
                "import diagnostic_support\n"
                "from diagnostic_support import CONTEXT_ENV\n"
                "CONTEXT_ENV=tuple(CONTEXT_ENV)+('CODEX_HOME','MINDIE_CODEX_BIN')\n"
                "from session_gate import generation_env\n"
                "child='import os,json;print(json.dumps({k:os.environ.get(k) "
                "for k in '+repr(CONTEXT_ENV)+'}))'\n"
                "result=subprocess.run([sys.executable,'-c',child],env=generation_env(),"
                "capture_output=True,text=True,check=True)\n"
                "worker=subprocess.run([sys.executable,str(Path(diagnostic_support.__file__)."
                "with_name('agent_worker.py')),'--identity'],env=generation_env(),"
                "capture_output=True,text=True,check=True)\n"
                "print(json.dumps(dict(native={k:os.environ.get(k) for k in CONTEXT_ENV},"
                "helper=json.loads(result.stdout),"
                "policy=json.loads(worker.stdout)['model'],"
                "worker_binary=json.loads(worker.stdout)['implementation']['native_command'],"
                "diagnostics_source=diagnostic_support.__file__)))\n"
            )
            # The host supplies only the bound adapter path; it filters the
            # diagnostic overrides just as the native Codex MCP launcher did.
            environment = {k: v for k, v in os.environ.items()
                           if k not in expected
                           and k not in {"PYTHONPATH", "_MINDIE_AGENT_DISPATCH_CONFIG"}}
            for revision in ("first", "updated"):
                scripts = root / "generations" / revision / "plugin/scripts"
                shutil.copytree(SCRIPTS, scripts,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                for name in ("bridge.py", "remote_bridge.py"):
                    (scripts / name).write_text(probe)
                (scripts.parent.parent / "ownership.json").write_text(json.dumps(
                    {"schema": "mindie-runtime-generation/2", "revision": revision}))
                config.write_text(json.dumps({
                    "runtime_scripts": str(scripts),
                    "diagnostics_context": selected,
                    "native_context": native,
                }))
                for role in ("knowledge-mcp", "remote-mcp", "stop"):
                    with self.subTest(revision=revision, role=role):
                        result = subprocess.run(
                            [sys.executable, str(root / "runtime_launcher.py"),
                             "--config", str(config), role],
                            env=environment, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout),
                                         {"native": expected, "helper": expected,
                                          "policy": "native-route/gpt-5.6-luna",
                                          "worker_binary": native["MINDIE_CODEX_BIN"],
                                          "diagnostics_source": str(scripts / "diagnostic_support.py")})
            # A retained or rolled-back generation predates the new helper API.
            # The stable launcher still restores the optional path context.
            (scripts / "diagnostic_support.py").write_text("LEGACY = True\n")
            legacy_probe = (
                "import json,os,diagnostic_support\n"
                "assert diagnostic_support.LEGACY\n"
                "print(json.dumps({key:os.environ.get(key) for key in "
                + repr(tuple(expected)) + "}))\n"
            )
            for name in ("bridge.py", "remote_bridge.py"):
                (scripts / name).write_text(legacy_probe)
            for role in ("knowledge-mcp", "remote-mcp", "stop"):
                result = subprocess.run(
                    [sys.executable, str(root / "runtime_launcher.py"), "--config",
                     str(config), role],
                    env=environment, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
            self.assertFalse((base / "policy.json").exists())
            self.assertFalse((base / "events").exists())


if __name__ == "__main__":
    unittest.main()
