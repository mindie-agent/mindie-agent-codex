#!/usr/bin/env python3
"""Hermetic tests for modelscope path, resume/status, and SHA256 decision logic.

Official ModelScope HTTP is replaced with fixtures. Nothing here opens a
network socket or depends on the developer HOME.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time

from pathlib import Path
for _p in Path(__file__).resolve().parents:
    if (_p / "domain-lib").is_dir():
        if str(_p / "domain-lib") not in sys.path:
            sys.path.insert(0, str(_p / "domain-lib"))
        break
else:
    raise RuntimeError("MindIE domain-lib not found; use the installed plugin")
ROOT = Path(__file__).resolve().parents[1]  # the skill package directory
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

# The skill scripts import ``requests`` at module load. Tests never call it;
# inject a stub so the suite stays hermetic without that extra package.
if "requests" not in sys.modules:
    sys.modules["requests"] = type(sys)("requests")
    sys.modules["requests"].Session = object  # type: ignore[attr-defined]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


auto = _load("vaws_modelscope_auto_under_test", "modelscope_auto.py")
download = _load("vaws_modelscope_download_under_test", "download_from_modelscope.py")
verify = _load("vaws_modelscope_verify_under_test", "verify_modelscope_sha256.py")
status_mod = _load("vaws_modelscope_status_under_test", "modelscope_download_status.py")


OFFICIAL = [
    {"Type": "blob", "Path": "config.json", "Size": 5, "Sha256": hashlib.sha256(b"hello").hexdigest()},
    {"Type": "blob", "Path": "weights.bin", "Size": 3, "Sha256": hashlib.sha256(b"abc").hexdigest()},
    {"Type": "blob", "Path": ".gitattributes", "Size": 1, "Sha256": "aa"},
]


def _spec(tmp: str, model_id: str = "org/name") -> auto.ModelSpec:
    return auto.ModelSpec(model_id=model_id, local_dir=Path(tmp) / "org" / "name")


def write_report(spec, files, revision="master"):
    with mock.patch.object(verify, "fetch_official_files", return_value=files):
        checks, summary = verify.verify_model(spec, revision, 4096, set(), {".gitattributes"})
    verify.write_outputs(spec.local_dir, "modelscope_sha256", checks, [summary])
    return checks


class ArgumentValidationTests(unittest.TestCase):
    def test_unrelated_or_reused_pid_is_not_an_active_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            pidfile = spec.local_dir / "download.pid"
            pid = 12345
            identity = {"started": "worker-start", "command": "python modelscope_auto.py worker"}
            # Exercise real record matching against a fixed OS snapshot. Native
            # process discovery belongs to the subprocess lifecycle tests.
            with mock.patch.object(auto, "process_identity",
                            side_effect=lambda queried: identity if queried == pid else None):
                for record in ({"pid": pid, "identity": {**identity, "started": "other"}},
                               {"pid": pid, "identity": {**identity, "command": "other"}}):
                    pidfile.write_text(json.dumps(record), encoding="utf-8")
                    with mock.patch.object(auto, "fetch_official_files", return_value=OFFICIAL[:2]):
                        self.assertEqual(auto.inspect_model(spec, "master")["state"], "needs-download")
                pidfile.write_text(json.dumps({"pid": pid, "identity": identity}), encoding="utf-8")
                self.assertTrue(auto.worker_is_active(spec.local_dir, pid))

    def test_aggregate_report_filters_model_and_directory_and_rejects_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            (spec.local_dir / "config.json").write_bytes(b"hello")
            (spec.local_dir / "weights.bin").write_bytes(b"abc")
            write_report(spec, OFFICIAL[:2])
            path = spec.local_dir / "modelscope_sha256.report.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            report["checks"].append({**report["checks"][0], "model_id": "other/model", "status": "mismatch"})
            report["checks"].append({**report["checks"][0], "local_dir": str(Path(tmp) / "elsewhere")})
            report["all_ok"] = False
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(auto.report_state(spec.local_dir, model_id=spec.model_id, revision="master", files=OFFICIAL[:2]), "ok")
            report["checks"][0]["local_dir"] = None
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(auto.report_state(spec.local_dir, model_id=spec.model_id, revision="master", files=OFFICIAL[:2]), "invalid")

    def test_parse_model_spec_requires_namespace_and_local_dir(self) -> None:
        spec = auto.parse_model_spec("org/name=/tmp/weights")
        self.assertEqual(spec.model_id, "org/name")
        self.assertEqual(spec.local_dir, Path("/tmp/weights"))
        for bad in ("org/name", "orgname=/tmp/x", "org/name=", "= /tmp/x"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                auto.parse_model_spec(bad)
        with self.assertRaises(argparse.ArgumentTypeError):
            status_mod.parse_model_spec("not-a-spec")
        with self.assertRaises(argparse.ArgumentTypeError):
            verify.parse_model_spec("org/name")

    def test_positive_int_rejects_zero_and_negative(self) -> None:
        self.assertEqual(auto.positive_int("3"), 3)
        self.assertEqual(download.positive_int("2"), 2)
        for bad in ("0", "-1"):
            with self.subTest(module="auto", bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                auto.positive_int(bad)
            with self.subTest(module="download", bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                download.positive_int(bad)

    def test_resolve_models_requires_model_or_root(self) -> None:
        with self.assertRaises(SystemExit):
            auto.resolve_models(argparse.Namespace(model=None, root=None))


class WorkerIdentityTests(unittest.TestCase):
    @staticmethod
    def args(spec):
        return argparse.Namespace(
            model=[spec], root=None, revision="master", proxy=None, no_proxy=False,
            max_retries=3, max_workers=None, download_parallels=1,
            parallel_threshold_mb=500, auto_install=False,
        )

    def test_launch_retries_identity_before_saving_and_detaching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            proc = mock.Mock(pid=4242)
            identity = {"started": "worker-start", "command": "python worker"}
            saved_on_detach = []

            @contextmanager
            def owner(*args, **kwargs):
                yield proc
                saved_on_detach.append(json.loads(
                    (spec.local_dir / "download.pid").read_text(encoding="utf-8")))

            with (
                mock.patch.object(auto, "_worker_process", side_effect=owner) as launch,
                mock.patch.object(auto, "process_identity", side_effect=[None, identity]) as observe,
                mock.patch.object(auto.time, "sleep") as sleep,
            ):
                self.assertEqual(auto.launch_worker(spec, self.args(spec), verify_only=False), proc.pid)
            launch.assert_called_once()
            self.assertEqual(observe.call_args_list, [mock.call(proc.pid), mock.call(proc.pid)])
            sleep.assert_called_once_with(.05)
            self.assertEqual(saved_on_detach, [{"pid": proc.pid, "identity": identity}])

    def test_persistent_launch_identity_failure_exits_owner_without_writing_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            pidfile = spec.local_dir / "download.pid"
            # A failed replacement must also preserve a previous ownership record.
            for previous in (None, b'{"pid": 17, "identity": {"started": "old", "command": "old"}}\n'):
                with self.subTest(previous_record=previous is not None):
                    if previous is not None:
                        pidfile.write_bytes(previous)
                    owner = mock.MagicMock()
                    owner.__enter__.return_value.pid = 4242
                    owner.__exit__.return_value = False
                    with (
                        mock.patch.object(auto, "_worker_process", return_value=owner),
                        mock.patch.object(auto, "process_identity", return_value=None) as observe,
                        mock.patch.object(auto.time, "sleep") as sleep,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "identify"):
                            auto.launch_worker(spec, self.args(spec), verify_only=False)
                    self.assertEqual(observe.call_count, 3)
                    self.assertEqual(sleep.call_args_list, [mock.call(.05), mock.call(.05)])
                    self.assertIs(owner.__exit__.call_args.args[0], RuntimeError)
                    self.assertEqual(pidfile.read_bytes() if pidfile.exists() else None, previous)

    def test_failed_posix_launch_cleans_its_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pids = Path(tmp) / "pids"
            cmd = [
                sys.executable,
                "-c",
                (
                    "import os, subprocess, sys, time\n"
                    f"root = {tmp!r}\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                    "open(root + '/pids', 'w', encoding='utf-8').write(f'{os.getpid()} {child.pid}')\n"
                    "time.sleep(30)\n"
                ),
            ]
            with self.assertRaisesRegex(RuntimeError, "record failed"):
                with auto._worker_process(cmd, env=os.environ.copy(), launch_log=subprocess.DEVNULL) as proc:
                    deadline = time.monotonic() + 5
                    while not pids.is_file() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(pids.is_file(), "owned worker did not start")
                    parent, child = (int(part) for part in pids.read_text(encoding="utf-8").split())
                    self.assertEqual(proc.pid, parent)
                    raise RuntimeError("record failed")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not auto.pid_is_active(parent) and not auto.pid_is_active(child):
                    break
                time.sleep(0.02)
            self.assertFalse(auto.pid_is_active(parent))
            self.assertFalse(auto.pid_is_active(child))

    def test_temporary_identity_failure_preserves_record_and_never_relaunches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            identity = {"started": "worker-start", "command": "python worker"}
            pidfile = spec.local_dir / "download.pid"
            original = json.dumps({"pid": 4242, "identity": identity}).encode("utf-8")
            pidfile.write_bytes(original)
            with (
                mock.patch.object(auto, "fetch_official_files", return_value=OFFICIAL[:2]),
                mock.patch.object(auto, "pid_is_active", return_value=True),
                mock.patch.object(auto, "process_identity", return_value=None) as observe,
                mock.patch.object(auto, "launch_worker") as launch,
            ):
                for command in (auto.command_status, auto.command_ensure):
                    with self.subTest(command=command.__name__), redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(command(self.args(spec)), 1)
                        self.assertIn("\tidentity-unavailable\t", output.getvalue())
                        self.assertEqual(pidfile.read_bytes(), original)
                observe.return_value = identity
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(auto.command_ensure(self.args(spec)), 0)
                self.assertIn("\tactive\t", output.getvalue())
                launch.assert_not_called()
                self.assertEqual(pidfile.read_bytes(), original)

    def test_live_pid_with_missing_identity_is_unknown_but_dead_pid_is_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            pidfile = spec.local_dir / "download.pid"
            for saved in (
                None, {}, {"started": "birth"}, {"command": "python worker"},
                {"started": 123, "command": "python worker"},
                {"started": "birth", "command": ["python", "worker"]},
            ):
                with self.subTest(identity=saved):
                    pidfile.write_text(json.dumps({"pid": 4242, "identity": saved}), encoding="utf-8")
                    with (
                        mock.patch.object(auto, "fetch_official_files", return_value=OFFICIAL[:2]),
                        mock.patch.object(auto, "pid_is_active", return_value=True) as alive,
                        mock.patch.object(auto, "process_identity", return_value={
                            "started": "birth", "command": "python worker",
                        }) as observe,
                        mock.patch.object(auto, "launch_worker") as launch,
                        redirect_stdout(io.StringIO()) as output,
                    ):
                        self.assertEqual(auto.command_ensure(self.args(spec)), 1)
                        self.assertIn("\tidentity-unavailable\t", output.getvalue())
                        launch.assert_not_called()
                        observe.assert_not_called()
                        alive.return_value = False
                        self.assertIs(auto.worker_is_active(spec.local_dir, 4242), False)
            pidfile.write_text("4242", encoding="utf-8")
            with mock.patch.object(auto, "pid_is_active", return_value=True):
                self.assertIsNone(auto.worker_is_active(spec.local_dir, 4242))


class PathHandlingTests(unittest.TestCase):
    def test_discover_models_reads_explicit_local_pid_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_dir = root / "Qwen" / "Qwen2.5-7B"
            model_dir.mkdir(parents=True)
            (model_dir / "download.pid").write_text("123\n", encoding="utf-8")
            (root / "download.pid").write_text("1\n", encoding="utf-8")
            specs = auto.discover_models(root)
            self.assertEqual([(s.model_id, s.local_dir) for s in specs], [("Qwen/Qwen2.5-7B", model_dir)])

    def test_local_size_counts_only_official_files_under_local_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_bytes(b"hello")
            (root / "extra.bin").write_bytes(b"xxxx")
            self.assertEqual(auto.local_size_for_files(root, OFFICIAL), 5)
            self.assertEqual(auto.local_size_for_files(root / "missing", OFFICIAL), 0)

    def test_configure_environment_stays_inside_explicit_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                no_proxy=True,
                proxy=None,
                download_parallels=2,
                parallel_threshold_mb=100,
                cache_dir=Path(tmp) / "cache",
            )
            old = os.environ.get("HTTP_PROXY")
            os.environ["HTTP_PROXY"] = "http://proxy.example.invalid:1"
            try:
                download.configure_environment(args)
                self.assertNotIn("HTTP_PROXY", os.environ)
                self.assertEqual(os.environ["MODELSCOPE_CACHE"], str(Path(tmp) / "cache"))
                self.assertEqual(os.environ["MODELSCOPE_DOWNLOAD_PARALLELS"], "2")
            finally:
                if old is None:
                    os.environ.pop("HTTP_PROXY", None)
                else:
                    os.environ["HTTP_PROXY"] = old


class StatusAndResumeTests(unittest.TestCase):
    def test_report_state_reads_local_verification_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            self.assertEqual(auto.report_state(local), "none")
            (local / "modelscope_sha256.report.json").write_text("{", encoding="utf-8")
            self.assertEqual(auto.report_state(local), "invalid")
            (local / "modelscope_sha256.report.json").write_text(
                json.dumps({"all_ok": True, "checks": []}), encoding="utf-8"
            )
            self.assertEqual(auto.report_state(local), "stale")
            (local / "modelscope_sha256.report.json").write_text(
                json.dumps(
                    {
                        "all_ok": False,
                        "checks": [{"status": "missing", "path": ".gitattributes"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(auto.report_state(local), "stale")
            (local / "modelscope_sha256.report.json").write_text(
                json.dumps(
                    {
                        "all_ok": False,
                        "checks": [{"status": "sha256_mismatch", "path": "weights.bin"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(auto.report_state(local), "stale")

    def test_inspect_model_state_machine_uses_local_dir_only(self) -> None:
        blobs = [item for item in OFFICIAL if item["Path"] != ".gitattributes"]
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            (spec.local_dir / "config.json").write_bytes(b"hello")
            (spec.local_dir / "weights.bin").write_bytes(b"abc")
            with (
                mock.patch.object(auto, "fetch_official_files", return_value=blobs),
                mock.patch.object(auto, "pid_is_active", return_value=False),
            ):
                complete = auto.inspect_model(spec, "master")
                self.assertEqual(complete["state"], "needs-verify")
                self.assertTrue(complete["complete"])
                write_report(spec, blobs)
                verified = auto.inspect_model(spec, "master")
                self.assertEqual(verified["state"], "verified")
            (spec.local_dir / "weights.bin").unlink()
            with (
                mock.patch.object(auto, "fetch_official_files", return_value=blobs),
                mock.patch.object(auto, "worker_is_active", return_value=True),
                mock.patch.object(auto, "read_pid", return_value=4242),
            ):
                active = auto.inspect_model(spec, "master")
            self.assertEqual(active["state"], "active")
            self.assertFalse(active["complete"])
            with (
                mock.patch.object(auto, "fetch_official_files", return_value=blobs),
                mock.patch.object(auto, "pid_is_active", return_value=False),
            ):
                missing = auto.inspect_model(spec, "master")
            self.assertEqual(missing["state"], "needs-download")

    def test_complete_requires_each_file_and_verification_expires_on_changes(self) -> None:
        blobs = [item for item in OFFICIAL if item["Path"] != ".gitattributes"]
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            (spec.local_dir / "config.json").write_bytes(b"12345678")
            with mock.patch.object(auto, "fetch_official_files", return_value=blobs):
                observed = auto.inspect_model(spec, "master")
                self.assertEqual(observed["actual"], observed["expected"])
                self.assertFalse(observed["complete"])
            (spec.local_dir / "config.json").write_bytes(b"hello")
            weights = spec.local_dir / "weights.bin"
            weights.write_bytes(b"abc")
            write_report(spec, blobs)
            def observed(revision="master", files=blobs):
                return auto.report_state(spec.local_dir, model_id=spec.model_id, revision=revision, files=files)
            self.assertEqual(observed(), "ok")
            self.assertEqual(observed("other-revision"), "stale")
            self.assertEqual(observed(files=[{**item, "Sha256": "new-upstream-hash"} for item in blobs]), "stale")
            self.assertEqual(auto.report_state(spec.local_dir, model_id="different/repo", revision="master", files=blobs), "stale")
            before = weights.stat()
            weights.write_bytes(b"abd")
            os.utime(weights, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            self.assertEqual(observed(), "stale")
            write_report(spec, blobs)
            self.assertEqual(observed(), "failed")
            weights.unlink()
            self.assertEqual(observed(), "stale")

    def test_auto_install_uses_isolated_uv_without_modifying_selected_python(self) -> None:
        with mock.patch.object(download.importlib.util, "find_spec", return_value=None), \
                mock.patch.object(download.shutil, "which", return_value="uv"), \
                mock.patch.object(download.subprocess, "call", return_value=7) as launch, \
                mock.patch.object(sys, "argv", ["download_from_modelscope.py", "--auto-install", "--model-id", "org/name", "--local-dir", "weights with spaces"]):
            with self.assertRaises(SystemExit) as stopped:
                download.ensure_modelscope(True)
        self.assertEqual(stopped.exception.code, 7)
        command = launch.call_args.args[0]
        self.assertEqual(command[:6], ["uv", "run", "--no-project", "--with", "modelscope", "python"])
        self.assertNotIn("--auto-install", command)
        self.assertEqual(command[-1], "weights with spaces")

    def test_command_ensure_launches_download_or_verify_or_stops(self) -> None:
        spec = auto.ModelSpec("org/name", Path("/tmp/org/name"))
        launched: list[bool] = []

        def fake_launch(_spec, _args, *, verify_only: bool) -> int:
            launched.append(verify_only)
            return 99

        args = argparse.Namespace(
            model=[spec],
            root=None,
            revision="master",
            proxy=None,
            no_proxy=False,
            max_retries=3,
            max_workers=None,
            download_parallels=1,
            parallel_threshold_mb=500,
            auto_install=False,
        )
        cases = [
            ("needs-download", False, "download-started", 1),
            ("needs-verify", True, "verify-started", 1),
            ("verified", None, "verified", 0),
            ("active", None, "active", 0),
        ]
        for state, expected_flag, expected_state, launches in cases:
            launched.clear()
            result = {
                "state": state,
                "verification": "none",
                "model_id": spec.model_id,
                "local_dir": str(spec.local_dir),
                "percent": 0.0,
                "actual": 0,
                "expected": 1,
                "pid": None,
            }
            with (
                mock.patch.object(auto, "resolve_models", return_value=[spec]),
                mock.patch.object(auto, "inspect_model", return_value=result),
                mock.patch.object(auto, "launch_worker", side_effect=fake_launch),
                mock.patch.object(auto, "print_status"),
            ):
                rc = auto.command_ensure(args)
            self.assertEqual(rc, 0)
            self.assertEqual(len(launched), launches)
            if launches:
                self.assertEqual(launched[0], expected_flag)
            self.assertEqual(result["state"], expected_state)

    def test_command_ensure_does_not_relaunch_failed_verify(self) -> None:
        spec = auto.ModelSpec("org/name", Path("/tmp/org/name"))
        result = {
            "state": "needs-verify",
            "verification": "failed",
            "model_id": spec.model_id,
            "local_dir": str(spec.local_dir),
            "percent": 100.0,
            "actual": 1,
            "expected": 1,
            "pid": None,
        }
        with (
            mock.patch.object(auto, "resolve_models", return_value=[spec]),
            mock.patch.object(auto, "inspect_model", return_value=result),
            mock.patch.object(auto, "launch_worker") as launch,
            mock.patch.object(auto, "print_status"),
        ):
            rc = auto.command_ensure(argparse.Namespace(model=[spec], root=None, revision="master"))
        self.assertEqual(rc, 0)
        launch.assert_not_called()
        self.assertEqual(result["state"], "verify-failed")


class Sha256DecisionTests(unittest.TestCase):
    def test_hashing_does_not_publish_success_when_file_changes_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = verify.ModelSpec("org/name", Path(tmp))
            path = spec.local_dir / "weights.bin"
            path.write_bytes(b"abc")
            original = verify.sha256_file
            def race(path, chunk_size):
                digest = original(path, chunk_size)
                before = path.stat()
                path.write_bytes(b"abd")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                return digest
            with mock.patch.object(verify, "sha256_file", side_effect=race):
                checks = write_report(spec, [OFFICIAL[1]])
            self.assertEqual(checks[0].status, "changed_during_verification")
            self.assertFalse(json.loads((spec.local_dir / "modelscope_sha256.report.json").read_text(encoding="utf-8"))["all_ok"])

    def test_verify_model_classifies_missing_size_hash_and_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = verify.ModelSpec("org/name", Path(tmp))
            (spec.local_dir / "config.json").write_bytes(b"hello")
            (spec.local_dir / "weights.bin").write_bytes(b"XXXX")
            (spec.local_dir / "stray.txt").write_text("nope", encoding="utf-8")
            official = [
                {
                    "Type": "blob",
                    "Path": "config.json",
                    "Size": 5,
                    "Sha256": hashlib.sha256(b"hello").hexdigest(),
                },
                {
                    "Type": "blob",
                    "Path": "weights.bin",
                    "Size": 3,
                    "Sha256": hashlib.sha256(b"abc").hexdigest(),
                },
                {
                    "Type": "blob",
                    "Path": "missing.bin",
                    "Size": 1,
                    "Sha256": "ab",
                },
            ]
            with mock.patch.object(verify, "fetch_official_files", return_value=official):
                checks, summary = verify.verify_model(
                    spec, "master", 4096, ignore_extra=set(), ignore_official=set()
                )
            by_path = {item.path: item.status for item in checks}
            self.assertEqual(by_path["config.json"], "ok")
            self.assertEqual(by_path["weights.bin"], "size_mismatch")
            self.assertEqual(by_path["missing.bin"], "missing")
            self.assertEqual(summary["ok"], 1)
            self.assertEqual(summary["size_mismatch"], 1)
            self.assertEqual(summary["missing"], 1)
            self.assertIn("stray.txt", summary["extra_files"])

            (spec.local_dir / "weights.bin").write_bytes(b"abc")
            with mock.patch.object(verify, "fetch_official_files", return_value=official[:2]):
                checks, summary = verify.verify_model(
                    spec, "master", 4096, ignore_extra={"stray.txt"}, ignore_official=set()
                )
            self.assertTrue(all(item.status == "ok" for item in checks))
            self.assertEqual(summary["extra_file_count"], 0)

            (spec.local_dir / "weights.bin").write_bytes(b"abd")
            with mock.patch.object(verify, "fetch_official_files", return_value=official[:2]):
                checks, _summary = verify.verify_model(
                    spec, "master", 4096, ignore_extra={"stray.txt"}, ignore_official=set()
                )
            self.assertEqual(
                {item.path: item.status for item in checks}["weights.bin"],
                "sha256_mismatch",
            )


if __name__ == "__main__":
    unittest.main()
