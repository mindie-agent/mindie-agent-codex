"""Runtime preparation evidence and stable release bindings, without native/model work."""
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stderr
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1]/'plugins/mindie-agent/scripts'
sys.path.insert(0,str(SCRIPTS))
import bootstrap
import entry
import native_entry
import product_contract
import summary_provider


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, MINDIE_DIAGNOSTICS_ROOT=temporary.name,
                                 MINDIE_DIAGNOSTICS_CONFIG=str(Path(temporary.name) / 'policy.json'))
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(self.close_diagnostics, Path(temporary.name).absolute())

    @staticmethod
    def close_diagnostics(root):
        from mindie_diagnostics import logging as diagnostic_logging
        # The test owns this temporary root; release its cached file handlers
        # before Windows attempts to remove the directory.
        with diagnostic_logging._LOCK:
            for key, recorder in list(diagnostic_logging._FAILURE_RECORDERS.items()):
                if Path(key[1]) == root:
                    recorder.close()
                    del diagnostic_logging._FAILURE_RECORDERS[key]

    def test_preparation_failure_preserves_static_stage_without_secret_or_double_record(self):
        import diagnostic_support
        private = 'PRIVATE_PROVIDER_CREDENTIAL'
        error = ValueError(private)
        output = io.StringIO()
        with patch.object(diagnostic_support, 'failure', return_value={'recorded': True}) as record, \
                redirect_stderr(output):
            with self.assertRaises(ValueError) as caught:
                with bootstrap.preparing('bootstrap'):
                    with bootstrap.preparing('candidate-validation'):
                        raise error
        self.assertIs(caught.exception, error)
        self.assertIn('candidate-validation: ValueError', output.getvalue())
        self.assertNotIn(private, output.getvalue())
        self.assertNotIn('at bootstrap', output.getvalue())
        record.assert_called_once()

    def test_session_hook_keeps_diagnostics_while_stop_stays_neutral(self):
        commands = native_entry.hook_commands('session-start')
        self.assertNotIn('2>&1', commands['command'])
        self.assertIn('2>&1', native_entry.hook_commands('stop')['command'])

    def test_short_stop_cannot_download_or_prepare(self):
        args=native_entry.command_args('0.0.1','stop')
        self.assertIn('--offline',args);self.assertIn('--no-python-downloads',args)
        self.assertNotIn('--offline',native_entry.command_args('0.0.1','knowledge-mcp'))
        with tempfile.TemporaryDirectory() as directory:
            env={'CODEX_HOME':directory}
            with patch.dict(os.environ,env),patch.object(sys,'argv',['loader','stop']):
                with self.assertRaisesRegex(RuntimeError,'not prepared'):
                    exec(native_entry.launcher_code('0.0.1'))
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_native_startup_window_covers_installation_without_changing_tool_timeouts(self):
        servers = json.loads((SCRIPTS.parent / ".mcp.json").read_text())["mcpServers"]
        for server in servers.values():
            self.assertEqual(server["startup_timeout_sec"], 600)
            self.assertNotIn("tool_timeout_sec", server)

    def test_fixed_entry_prefers_prepared_release_after_cache_eviction(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory); fixed=home/'plugins/data/mindie-agent-mindie-agent/releases/0.0.1/entry.py'
            fixed.parent.mkdir(parents=True);fixed.write_text("RESULT='fixed'\n")
            with patch.dict(os.environ,{'CODEX_HOME':directory}), \
                 patch.object(sys,'argv',['loader','stop']), \
                 patch('runpy.run_path',return_value={}) as run:
                exec(native_entry.launcher_code('0.0.1'))
            self.assertEqual(run.call_args.args[0],str(fixed))

    def test_publish_entry_is_immutable_and_has_no_current_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            release=Path(directory)
            target=bootstrap.publish_entry(Path(sys.executable),SCRIPTS,release)
            original=target.read_bytes()
            self.assertNotIn(b'current',original)
            bootstrap.publish_entry(Path(sys.executable),SCRIPTS,release)
            with self.assertRaisesRegex(ValueError,'immutable release entry'):
                bootstrap.publish_entry(Path('/different/python'),SCRIPTS,release)
            self.assertEqual(target.read_bytes(),original)

    def test_existing_completed_runtime_never_repairs_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory);source=SCRIPTS.parent
            release=data/'releases'/product_contract.native_version(source);release.mkdir(parents=True)
            key=bootstrap.runtime_key(source)
            python=data/'runtimes'/key/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            (release/'runtime.json').write_text(json.dumps({'key':key,'python':str(python),'validation':{}}))
            with patch.object(bootstrap,'readback',side_effect=ValueError('corrupt installed runtime')), \
                 patch.object(bootstrap,'run') as install:
                with self.assertRaisesRegex(ValueError,'corrupt installed'):
                    bootstrap.ensure_runtime(source,data)
                install.assert_not_called()

    def test_existing_runtime_keeps_its_full_digest_path_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            source = SCRIPTS.parent
            release = data / "releases" / product_contract.native_version(source)
            release.mkdir(parents=True)
            key = bootstrap.runtime_key(source)
            python = data / "runtimes" / key / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python")
            validation = dict(product_contract.identity(source), status="validated")
            receipt = release / "runtime.json"
            receipt.write_text(json.dumps(
                dict(key=key, python=str(python), validation=validation)))
            fixed_entry = bootstrap.publish_entry(python, SCRIPTS, release)
            original = receipt.read_bytes(), fixed_entry.read_bytes()
            with patch.object(bootstrap, "readback", return_value=SCRIPTS) as readback, \
                    patch.object(bootstrap, "run") as install:
                self.assertEqual(bootstrap.ensure_runtime(source, data),
                                 (python, SCRIPTS, release))
                readback.assert_called_once_with(python, source)
                install.assert_not_called()
            self.assertEqual((receipt.read_bytes(), fixed_entry.read_bytes()), original)

    def test_source_files_exclude_build_caches_but_include_lock_and_resources(self):
        names={p.relative_to(SCRIPTS.parent).as_posix() for p in product_contract.package_files(SCRIPTS.parent)}
        self.assertTrue({'uv.lock','pyproject.toml','product-contract.json','scripts/entry.py','skills/mindie-agent/references/user-notice.md'}<=names)
        self.assertFalse(any('build/' in x or '.egg-info/' in x or '__pycache__' in x for x in names))

    def test_required_context_does_not_require_optional_native_auth_or_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)
            with patch.dict(os.environ,{},clear=True):
                summary_provider.required_environment(home)
                (home/'config.toml').write_text('model_provider="fixture"\n[model_providers.fixture]\nenv_key="REQUIRED_FIXTURE_KEY"\n')
                with self.assertRaisesRegex(ValueError,'required provider context unavailable'):
                    summary_provider.required_environment(home)
                with patch.dict(os.environ,{'REQUIRED_FIXTURE_KEY':'private-never-recorded'}):
                    summary_provider.required_environment(home)

    def test_stop_does_not_prepare_missing_release_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);release=root/'release';release.mkdir()
            (release/'runtime.json').write_text(json.dumps({'key':'fixture','python':sys.executable,'validation':{}}))
            base=root/'missing-parent/adapter.json'
            with patch.dict(os.environ,{'MINDIE_AGENT_CONFIG':str(base)}), \
                 patch('capture_config.prepare',side_effect=AssertionError('Stop preparation')), \
                 patch.object(entry,'install',side_effect=AssertionError('Stop installation')):
                with self.assertRaisesRegex(RuntimeError,'not prepared'):
                    entry.profile(release,prepare=False)
            self.assertFalse(base.parent.exists())

    def test_cold_mcp_waits_for_profile_publication_but_stop_does_not(self):
        import subprocess
        from contextlib import ExitStack
        from tests.process_fixtures import owned_fixture_process
        from update_lock import update_lock

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = os.path.abspath(sys.executable)
            release = root / "release"
            release.mkdir()
            (release / "runtime.json").write_text(json.dumps(
                dict(python=python, validation={})))
            config, engine = root / "adapter.json", root / "engine.json"
            adapter = dict(python=python, runtime_scripts=str(SCRIPTS),
                           engine_config=str(engine), product_validation={})
            engine.write_text(json.dumps(dict(
                transcript_adapter=str(SCRIPTS / "codex_transcript.py"), product_validation={})))
            config.write_text("{")  # Real installation publishes while holding its exclusive lock.
            waiting, dispatched = root / "waiting", root / "dispatched"
            common = (
                "import sys,os;from pathlib import Path;"
                f"sys.path.insert(0,{str(SCRIPTS)!r});import entry,session_gate;"
                f"os.environ['MINDIE_AGENT_CONFIG']={str(config)!r};"
            )
            initialize = common + (
                "from contextlib import contextmanager\n"
                "lock=entry.update_lock\n"
                "@contextmanager\n"
                "def observed(*args,**kwargs):\n"
                f" Path({str(waiting)!r}).touch()\n"
                " with lock(*args,**kwargs):yield\n"
                "entry.update_lock=observed\n"
                "session_gate.runtime_env=lambda _:{}\n"
                f"entry.dispatch=lambda *_:Path({str(dispatched)!r}).touch()\n"
                f"sys.argv=['entry','--release',{str(release)!r},'knowledge-mcp']\n"
                "entry.main()\n"
            )
            with ExitStack() as children:
                with update_lock(config, exclusive=True):
                    process = children.enter_context(owned_fixture_process(
                        [python, "-c", initialize],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                    until = time.monotonic() + 10
                    while not waiting.exists() and time.monotonic() < until:
                        if process.poll() is not None:
                            stdout, stderr = process.communicate(timeout=10)
                            self.fail(f"initializer failed before lock acquisition: {stdout!r} {stderr!r}")
                        time.sleep(.01)
                    self.assertTrue(waiting.exists())
                    self.assertIsNone(process.poll())
                    self.assertFalse(dispatched.exists())
                    stop = subprocess.run(
                        [python, "-c", common +
                         f"\ntry:entry.profile(Path({str(release)!r}),prepare=False)\n"
                         "except BlockingIOError:print('stop-lock-unavailable')\n"],
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(stop.returncode, 0, stop.stderr)
                    self.assertEqual(stop.stdout.strip(), "stop-lock-unavailable")
                    config.write_text(json.dumps(adapter))
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertTrue(dispatched.exists())

    def test_warm_stop_checks_exact_profile_without_source_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()
            receipt = {"key": "fixture", "python": sys.executable, "validation": {}}
            (release / "runtime.json").write_text(json.dumps(receipt))
            config, engine = root / "adapter.json", root / "engine.json"
            adapter = dict(python=sys.executable, runtime_scripts=str(entry.SCRIPTS),
                           engine_config=str(engine), product_validation={})
            config.write_text(json.dumps(adapter))
            engine.write_text(json.dumps(dict(transcript_adapter=str(entry.SCRIPTS / "codex_transcript.py"),
                                              product_validation={})))
            before = config.read_bytes(), engine.read_bytes()
            with patch.dict(os.environ, {"MINDIE_AGENT_CONFIG": str(config)}), \
                    patch.object(product_contract, "identity", side_effect=AssertionError("warm source hash")):
                self.assertEqual(entry.profile(release, prepare=False), config)
                self.assertEqual(entry.profile(release, SCRIPTS.parent), config)
                adapter["python"] = str(root / "other" / "runtime")
                config.write_text(json.dumps(adapter))
                self.assertEqual(entry.profile(release), config)
                self.assertEqual(json.loads(config.read_text()), adapter)
                self.assertEqual(engine.read_bytes(), before[1])

    def test_development_engine_is_rejected_without_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()
            (release / "runtime.json").write_text(json.dumps(dict(python=sys.executable, validation={})))
            config, engine = root / "adapter.json", root / "engine.json"
            adapter = dict(python=sys.executable, runtime_scripts=str(entry.SCRIPTS),
                           engine_config=str(engine), product_validation={})
            config.write_text(json.dumps(adapter))
            engine.write_text(json.dumps(dict(client_contract={}, product_validation={},
                transcript_adapter=str(entry.SCRIPTS / "codex_transcript.py"))))
            before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
            with patch.dict(os.environ, {"MINDIE_AGENT_CONFIG": str(config)}):
                with self.assertRaisesRegex(ValueError, "unsupported installation"):
                    entry.profile(release)
            self.assertEqual(
                {p.name: p.read_bytes() for p in root.iterdir()
                 if p.is_file() and p.suffix != ".lock"}, before)

    def test_installer_failure_keeps_private_output_and_never_publishes_ready(self):
        from bounded_process import run
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            calls = []

            def synthetic_installer(command, payload, **kwargs):
                calls.append(command)
                self.assertEqual(command[1:3], ["-I", "-c"])
                key = bootstrap.runtime_key(SCRIPTS.parent)
                runtime = data / "runtimes" / (key[:24] if os.name == "nt" else key)
                self.assertEqual(kwargs["env"]["UV_PROJECT_ENVIRONMENT"], str(runtime))
                log = command[4]
                # Exercise the actual descriptor redirection and owned runner;
                # replace only uv's executable/args with a no-network process.
                actual = [*command[:5], sys.executable, "-c",
                          "import sys;print('PRIVATE_INSTALL_DETAIL',file=sys.stderr);raise SystemExit(17)"]
                return run(actual, payload, **kwargs)

            with patch.object(bootstrap.shutil, "which", return_value=sys.executable), \
                    patch.object(bootstrap, "run", side_effect=synthetic_installer), \
                    patch.object(bootstrap, "readback", side_effect=AssertionError("failed install")):
                with self.assertRaises(RuntimeError) as caught:
                    bootstrap.ensure_runtime(SCRIPTS.parent, data)
            self.assertEqual(len(calls), 1)
            self.assertEqual(caught.exception.process_result.execution, "completed")
            self.assertEqual(caught.exception.process_result.returncode, 17)
            self.assertEqual(caught.exception.process_result.cleanup, [])
            self.assertEqual(len(list(data.rglob("prepare-*.log"))), 1)
            self.assertIn("PRIVATE_INSTALL_DETAIL", next(data.rglob("prepare-*.log")).read_text())
            self.assertEqual(list(data.rglob("runtime.json")), [])
            self.assertEqual(list(data.rglob("entry.py")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX signal owner-death contract")
    def test_bootstrap_owner_death_stops_installer_and_build_before_ready(self):
        import signal
        import subprocess
        from tests.process_fixtures import owned_fixture_process
        from update_lock import file_lock

        for interruption in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=interruption.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                data, ready = root / "data", root / "ready.json"
                installer_lock, build_lock = root / "installer.lock", root / "build.lock"
                build_ready = root / "build-ready"
                build = (
                    "import sys,time;from pathlib import Path;"
                    f"sys.path.insert(0,{str(SCRIPTS)!r});from bootstrap_lock import exclusive_lock;"
                    f"\nwith exclusive_lock({str(build_lock)!r}):"
                    f"\n Path({str(build_ready)!r}).touch();time.sleep(30)\n"
                )
                installer = (
                    "import os,sys,time,json,subprocess;from pathlib import Path;"
                    f"sys.path.insert(0,{str(SCRIPTS)!r});from bootstrap_lock import exclusive_lock;"
                    f"\nwith exclusive_lock({str(installer_lock)!r}):"
                    f"\n child=subprocess.Popen([sys.executable,'-c',{build!r}])"
                    f"\n while not Path({str(build_ready)!r}).exists(): time.sleep(.01)"
                    f"\n Path({str(ready)!r}).write_text(json.dumps(dict(group=os.getpgrp(),pid=os.getpid(),build=child.pid)))"
                    "\n time.sleep(30)\n"
                )
                owner = (
                    "import sys;from pathlib import Path;"
                    f"sys.path.insert(0,{str(SCRIPTS)!r});import bootstrap;"
                    "owned=bootstrap.run;"
                    "bootstrap.shutil.which=lambda _:sys.executable;"
                    f"bootstrap.run=lambda argv,payload,**kwargs:owned([*argv[:5],sys.executable,'-c',{installer!r}],payload,**kwargs);"
                    f"bootstrap.ensure_runtime(Path({str(SCRIPTS.parent)!r}),Path({str(data)!r}))"
                )
                with owned_fixture_process(
                    [sys.executable, "-c", owner], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                ) as process:
                    try:
                        until = time.monotonic() + 10
                        while not ready.exists() and time.monotonic() < until:
                            self.assertIsNone(process.poll(), "bootstrap exited before installer readiness")
                            time.sleep(.01)
                        self.assertTrue(ready.exists(), "installer/build readiness handshake did not complete")
                        for lock in (installer_lock, build_lock):
                            with self.assertRaises(BlockingIOError):
                                with file_lock(lock, exclusive=True):
                                    pass
                        process.send_signal(interruption)
                        self.assertEqual(process.wait(timeout=3), -interruption)
                        for lock in (installer_lock, build_lock):
                            until = time.monotonic() + 3
                            while True:
                                try:
                                    with file_lock(lock, exclusive=True):
                                        break
                                except BlockingIOError:
                                    if time.monotonic() >= until:
                                        self.fail("installer/build survived bootstrap owner death")
                                    time.sleep(.01)
                        self.assertEqual(list(data.rglob("runtime.json")), [])
                        self.assertEqual(list(data.rglob("entry.py")), [])
                        self.assertEqual(len(list(data.rglob("prepare-*.log"))), 1)
                    finally:
                        # Only this fixture's owned group is eligible cleanup;
                        # the assertions above ran before this safety cleanup.
                        if ready.exists():
                            group = json.loads(ready.read_text())["group"]
                            try:
                                os.killpg(group, signal.SIGKILL)
                            except ProcessLookupError:
                                pass

    def test_loaded_mcp_delegates_whole_current_helper_after_switch(self):
        import mcp_gate
        import session_gate
        self.addCleanup(session_gate.bind_explicit_config, None)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as owned:
            root = Path(directory)
            config = root / "adapter.json"
            old, new = root / "old", root / "new"
            old.mkdir(); new.mkdir()
            for path, text in ((old, "old"), (new, "new")):
                (path / "runtime_call.py").write_text(
                    "import json,sys;print(json.dumps({'content':[{'type':'text','text':" + repr(text)
                    + "}],'structuredContent':{'python':sys.executable,'helper':__file__}}))")
            original = dict(python=os.path.abspath(sys.executable), runtime_scripts=str(old),
                            engine_config=str(root / "engine.json"))
            config.write_text(json.dumps(original))
            with patch.dict(os.environ, {"MINDIE_AGENT_CONFIG": str(config),
                                         "MINDIE_REMOTE_STATE_DIR": str(root / "receipts")}), \
                    patch.object(mcp_gate, "runtime_env", return_value=dict(os.environ)):
                gate = mcp_gate.Gate("remote")
                owned.callback(lambda: [
                    receipt._ownership.close() for receipt in gate._remote_receipts.values()
                ])
                request = {"id": 1, "params": {"name": "remote_read", "arguments": {"host": "fixture", "path": "/"}}}
                first = gate.call(request, cli_identity=("task", None))
                self.assertEqual(first["content"][0]["text"], "old")
                config.write_text(json.dumps(dict(original, runtime_scripts=str(new))))
                request["id"] = 2
                second = gate.call(request, cli_identity=("task", None))
                self.assertEqual(second["content"][0]["text"], "new")
                self.assertEqual(second["structuredContent"]["helper"], str(new / "runtime_call.py"))
                self.assertEqual(second["structuredContent"]["python"], os.path.abspath(sys.executable))

    def test_loaded_mcp_keeps_explicit_mutable_choices_on_the_same_binding(self):
        import mcp_gate
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "adapter.json"
            original = dict(python=sys.executable, runtime_scripts=str(SCRIPTS),
                            engine_config=str(Path(directory) / "engine.json"))
            config.write_text(json.dumps(original))
            with patch.object(mcp_gate, "config_path", return_value=config):
                gate = mcp_gate.Gate("remote")
                config.write_text(json.dumps(dict(original, diagnostics_context={"new": "choice"})))
                with patch.object(gate, "_remote_call", return_value={"isError": False}) as dispatch:
                    self.assertEqual(gate.call({}), {"isError": False})
                    dispatch.assert_called_once()

    def test_switch_gate_rejects_before_remote_receipt_or_dispatch(self):
        import mcp_gate
        from update_lock import update_lock
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "adapter.json"
            config.write_text("{}")
            with patch.object(mcp_gate, "config_path", return_value=config):
                gate = mcp_gate.Gate("remote")
                with update_lock(config, exclusive=True), \
                        patch.object(gate, "_remote_call", side_effect=AssertionError("admitted")):
                    result = gate.call({})
            self.assertEqual(result["structuredContent"]["code"], "temporary_unavailable")
            self.assertEqual(result["structuredContent"]["execution"], "not_started")

    def test_native_stop_rechecks_binding_under_lock_before_dispatch(self):
        import session_gate
        self.addCleanup(session_gate.bind_explicit_config, None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()
            (release / "runtime.json").write_text(json.dumps(
                dict(python=sys.executable, validation={})))
            config = root / "adapter.json"
            engine = root / "engine.json"
            engine.write_text(json.dumps({"transcript_adapter": "/wrong/adapter.py", "product_validation": {}}))
            config.write_text(json.dumps(dict(python=str(root / "replacement" / "runtime"), engine_config=str(engine),
                runtime_scripts=str(SCRIPTS), product_validation={})))
            with patch.dict(os.environ, {"MINDIE_AGENT_CONFIG": str(config)}), \
                    patch.object(sys, "argv", ["entry", "--release", str(release), "stop"]), \
                    patch.object(entry, "profile", return_value=config), \
                    patch.object(entry, "dispatch", side_effect=AssertionError("capture")):
                with self.assertRaisesRegex(ValueError, "unsupported installation"):
                    entry.main()
