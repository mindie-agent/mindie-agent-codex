"""Legacy installed-module recovery boundaries, with real OS locks and child processes."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))


def legacy_scripts(tag, root):
    archive = subprocess.run(
        ["git", "archive", tag, "plugins/mindie-agent"],
        cwd=REPO, capture_output=True, check=True, timeout=20)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as stream:
        stream.extractall(root, filter="data")
    scripts = Path(root) / "plugins/mindie-agent/scripts"
    if not (scripts / "entry.py").is_file():
        raise AssertionError(f"{tag} did not provide a real installed entry")
    return scripts


def legacy_profile(root, scripts):
    root = Path(root)
    release = root / "release"
    release.mkdir()
    (release / "runtime.json").write_text(json.dumps(
        dict(python=sys.executable, validation={})), encoding="utf-8")
    engine = root / "engine.json"
    engine.write_text(json.dumps(dict(
        transcript_adapter=str(scripts / "codex_transcript.py"),
        product_validation={})), encoding="utf-8")
    config = root / "codex.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, runtime_scripts=str(scripts),
        engine_config=str(engine), product_validation={},
        native_context=dict(CODEX_HOME=str(root / "native"),
                            MINDIE_CODEX_BIN=sys.executable))),
        encoding="utf-8")
    updates = root / "codex.updates"
    updates.mkdir()
    (updates / "status.json").write_text(json.dumps(dict(
        status="up_to_date", checked_at=time.time(),
        last_successful_check=time.time() + 3600)), encoding="utf-8")
    (root / "native").mkdir()
    return config, release


def environment(root, config):
    return dict(os.environ, MINDIE_AGENT_CONFIG=str(config),
                MINDIE_DIAGNOSTICS_ROOT=str(Path(root) / "diagnostics"),
                MINDIE_DIAGNOSTICS_CONFIG=str(Path(root) / "diagnostics.json"),
                PYTHONIOENCODING="utf-8")


def lock_probe(scripts, config):
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from update_lock import update_lock\n"
        "try:\n"
        " with update_lock(Path(sys.argv[2]), exclusive=True): print('exclusive')\n"
        "except BlockingIOError: print('blocked')\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(scripts), str(config)],
        capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class ImmutableActivationAttemptTests(unittest.TestCase):
    def test_delayed_child_ignores_replaced_pending_and_cannot_downgrade(self):
        from capture_config import prepare as capture_prepare
        from native_context import service_launch_context
        import release_activation
        from product_contract import identity
        from session_gate import runtime_binding

        with tempfile.TemporaryDirectory(prefix="mindie-immutable-attempt-") as folder:
            home = Path(folder)
            config = home / "codex.json"
            keys = ("CODEX_HOME", "MINDIE_AGENT_CONFIG", "MINDIE_DIAGNOSTICS_ROOT",
                    "MINDIE_DIAGNOSTICS_CONFIG")
            saved = {key: os.environ.get(key) for key in keys}
            self.addCleanup(lambda: [os.environ.pop(key, None) if value is None
                                     else os.environ.__setitem__(key, value)
                                     for key, value in saved.items()])
            os.environ.update(
                CODEX_HOME=str(home), MINDIE_AGENT_CONFIG=str(config),
                MINDIE_DIAGNOSTICS_ROOT=str(home / "diagnostics"),
                MINDIE_DIAGNOSTICS_CONFIG=str(home / "reporting.json"))
            validation = dict(identity(SCRIPTS.parent), status="validated",
                              native_version="0.1.9")
            adapter = dict(
                python=sys.executable, runtime_scripts=str(SCRIPTS),
                engine_config=str(home / "old.engine.json"),
                product_validation=validation,
                community_config=str(home / "mindie-community.json"),
                admission_path=str(home / "admission.sqlite3"),
                native_context={"CODEX_HOME": str(home)}, domain_configs={})
            engine = dict(
                root=str(home / "data"), domain="vllm-ascend",
                admission_path=adapter["admission_path"],
                community_config=adapter["community_config"],
                transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
                product_validation=validation,
                service_launch_context=service_launch_context(config, adapter),
                **capture_prepare(sys.executable, SCRIPTS))
            Path(adapter["engine_config"]).write_text(json.dumps(engine), encoding="utf-8")
            config.write_text(json.dumps(adapter), encoding="utf-8")

            def release(version):
                target = home / version
                target.mkdir()
                (target / "runtime.json").write_text(json.dumps(dict(
                    python=sys.executable,
                    validation=dict(validation, native_version=version))),
                    encoding="utf-8")
                return target

            old_target, new_target = release("0.1.10"), release("0.1.11")
            pending = home / "native.json"
            old_expected = runtime_binding(adapter)
            pending.write_text(json.dumps(dict(
                metadata={"version": "0.1.10"},
                activation={"status": "unconfirmed", "expected_binding": old_expected})),
                encoding="utf-8")
            barrier, ready = home / "release-child", home / "child-ready"
            child_code = home / "delayed_child.py"
            child_code.write_text(
                "import json,sys,time\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from release_activation import main\n"
                "barrier, ready = Path(sys.argv[2]), Path(sys.argv[3])\n"
                "ready.write_text('ready')\n"
                "while not barrier.exists(): time.sleep(.01)\n"
                "print(json.dumps(main(['release_activation.py', sys.argv[4], sys.argv[5], '--attempt'],\n"
                "                    stdin=sys.stdin.buffer)))\n")
            child = subprocess.Popen(
                [sys.executable, "-I", "-B", str(child_code), str(SCRIPTS),
                 str(barrier), str(ready), str(config), str(old_target)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            try:
                until = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < until:
                    if child.poll() is not None:
                        self.fail(child.stderr.read())
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                child.stdin.write(json.dumps(dict(
                    schema=release_activation.ATTEMPT_SCHEMA,
                    expected_binding=old_expected)))
                child.stdin.close()
                successor = release_activation.activate(
                    config, release_activation.prepare(config, new_target))
                self.assertEqual(successor["status"], "active")
                advanced = json.loads(config.read_text(encoding="utf-8"))
                self.assertEqual(
                    advanced["product_validation"]["native_version"], "0.1.11")
                pending.write_text(json.dumps(dict(
                    metadata={"version": "0.1.12"},
                    activation={"status": "unconfirmed",
                                "expected_binding": runtime_binding(advanced)})),
                    encoding="utf-8")
                barrier.write_text("release\n")
                self.assertEqual(child.wait(timeout=20), 0)
                result = json.loads(child.stdout.read())
                self.assertEqual(result["status"], "deferred")
                self.assertEqual(result["reason"], "installation_changed")
                self.assertEqual(
                    json.loads(config.read_text(encoding="utf-8"))["product_validation"]["native_version"],
                    "0.1.11")
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
                child.stdout.close()
                child.stderr.close()


class LegacyEntryLockTests(unittest.TestCase):
    def test_real_old_tools_mcp_holds_idle_lock_until_eof(self):
        for tag in ("v0.1.8", "v0.1.9"):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                scripts = legacy_scripts(tag, root / "legacy")
                config, release = legacy_profile(root, scripts)
                env = environment(root, config)
                process = subprocess.Popen(
                    [sys.executable, "-I", str(scripts / "entry.py"),
                     "--release", str(release), "tools-mcp"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, env=env)
                try:
                    until = time.monotonic() + 8
                    while time.monotonic() < until:
                        if process.poll() is not None:
                            error = process.stderr.read().decode("utf-8", "replace")
                            self.fail(f"{tag} tools-mcp exited before serving: {error}")
                        if lock_probe(scripts, config) == "blocked":
                            break
                        time.sleep(0.05)
                    else:
                        self.fail(f"{tag} tools-mcp never acquired the idle lock")
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=10), 0)
                    self.assertEqual(lock_probe(scripts, config), "exclusive")
                finally:
                    if process.poll() is None:
                        process.kill()
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
                    process.wait(timeout=10)
                    process.stderr.close()

    def test_real_old_controller_returns_permanent_unknown_before_any_selection(self):
        for tag in ("v0.1.8", "v0.1.9"):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                scripts = legacy_scripts(tag, Path(directory))
                code = (
                    "import json,sys\n"
                    "sys.path.insert(0, sys.argv[1])\n"
                    "import harness_update\n"
                    "pending={'source':'/not-read','activation':"
                    "{'status':'unknown','process':None}}\n"
                    "print(json.dumps(harness_update._prepare_and_activate("
                    "'/none', pending, {}, __import__('pathlib').Path(sys.argv[2]))))\n"
                )
                result = subprocess.run(
                    [sys.executable, "-I", "-c", code, str(scripts), directory],
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertEqual(value["status"], "unknown")
                self.assertEqual(value["stage"], "activation_writer")
                self.assertEqual(value["activation"],
                                 dict(status="unknown", process=None))


if __name__ == "__main__":
    unittest.main()
