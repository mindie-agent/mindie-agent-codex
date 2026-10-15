"""Runtime preparation evidence and stable release bindings, without native/model work."""
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
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
        commands = native_entry.hook_commands('0.0.1', 'session-start')
        self.assertNotIn('2>&1', commands['command'])
        self.assertIn('2>&1', native_entry.hook_commands('0.0.1', 'stop')['command'])

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
            release=data/'releases/0.0.1';release.mkdir(parents=True)
            key=bootstrap.runtime_key(source)
            python=data/'runtimes'/key/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            (release/'runtime.json').write_text(json.dumps({'key':key,'python':str(python),'validation':{}}))
            with patch.object(bootstrap,'readback',side_effect=ValueError('corrupt installed runtime')), \
                 patch.object(bootstrap,'run') as install:
                with self.assertRaisesRegex(ValueError,'corrupt installed'):
                    bootstrap.ensure_runtime(source,data)
                install.assert_not_called()

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

    def test_warm_stop_checks_exact_profile_without_lock_or_source_hash(self):
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
                    patch.object(entry, "update_lock", side_effect=AssertionError("warm lock")), \
                    patch.object(product_contract, "identity", side_effect=AssertionError("warm source hash")):
                self.assertEqual(entry.profile(release, prepare=False), config)
                self.assertEqual(entry.profile(release, SCRIPTS.parent), config)
                adapter["python"] = "/other/runtime"
                config.write_text(json.dumps(adapter))
                with self.assertRaisesRegex(ValueError, "unsupported_development_replacement"):
                    entry.profile(release)
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
                with self.assertRaisesRegex(ValueError, "unsupported development"):
                    entry.profile(release)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}, before)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_nonregular_profile_is_not_first_install_or_a_blocking_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()
            (release / "runtime.json").write_text(json.dumps(dict(python=sys.executable, validation={})))
            config = root / "adapter.json"
            os.mkfifo(config)
            with patch.dict(os.environ, {"MINDIE_AGENT_CONFIG": str(config)}), \
                    patch.object(entry, "install", side_effect=AssertionError("new install")):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    entry.profile(release)
            self.assertFalse((root / "adapter.update.lock").exists())

    def test_installer_failure_keeps_private_output_and_never_publishes_ready(self):
        from bounded_process import run
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            calls = []

            def synthetic_installer(command, payload, **kwargs):
                calls.append(command)
                self.assertEqual(command[1:3], ["-I", "-c"])
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
                    f"sys.path.insert(0,{str(SCRIPTS)!r});from update_lock import file_lock;"
                    f"\nwith file_lock({str(build_lock)!r},exclusive=True):"
                    f"\n Path({str(build_ready)!r}).touch();time.sleep(30)\n"
                )
                installer = (
                    "import os,sys,time,json,subprocess;from pathlib import Path;"
                    f"sys.path.insert(0,{str(SCRIPTS)!r});from update_lock import file_lock;"
                    f"\nwith file_lock({str(installer_lock)!r},exclusive=True):"
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

    def test_loaded_mcp_rejects_replaced_binding_before_receipts_or_dispatch(self):
        import mcp_gate
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "adapter.json"
            original = dict(python=sys.executable, runtime_scripts=str(SCRIPTS),
                            engine_config=str(Path(directory) / "engine.json"),
                            product_validation={"source_sha256": "fixed"})
            config.write_text(json.dumps(original))
            with patch.object(mcp_gate, "config_path", return_value=config):
                knowledge = mcp_gate.Gate("knowledge")
                remote = mcp_gate.Gate("remote")
                replacement = dict(original, python="/different/runtime")
                config.write_text(json.dumps(replacement))
                for gate in (knowledge, remote):
                    with self.subTest(surface=gate.surface), \
                            patch.object(gate, "_call", side_effect=AssertionError("dispatch")), \
                            patch.object(gate, "_remote_call", side_effect=AssertionError("receipt claim")):
                        result = gate.call({}, cli_identity=("task", None))
                        self.assertTrue(result["isError"])
                        self.assertIn("reload_required", result["content"][0]["text"])
                self.assertEqual(json.loads(config.read_text()), replacement)
                self.assertEqual(sorted(path.name for path in Path(directory).iterdir()),
                                 ["adapter.json", "adapter.update.lock"])

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

    def test_mcp_constructed_after_replacement_keeps_native_entry_identity(self):
        import mcp_gate
        import session_gate
        self.addCleanup(session_gate.bind_explicit_config, None)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "adapter.json"
            original = dict(python=sys.executable, runtime_scripts=str(SCRIPTS),
                            engine_config=str(Path(directory) / "engine.json"))
            config.write_text(json.dumps(original))
            with patch.dict(os.environ, {"MINDIE_AGENT_CONFIG": str(config)}):
                session_gate.bind_explicit_config(
                    config, binding=session_gate.runtime_binding(original))
                config.write_text(json.dumps(dict(original, python="/replacement/runtime")))
                gate = mcp_gate.Gate("remote")
                with patch.object(gate, "_remote_call", side_effect=AssertionError("receipt claim")):
                    result = gate.call({})
                self.assertTrue(result["isError"])
                self.assertIn("reload_required", result["content"][0]["text"])

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
            config.write_text(json.dumps(dict(python="/replacement/runtime",
                runtime_scripts=str(SCRIPTS), product_validation={})))
            with patch.dict(os.environ, {"MINDIE_AGENT_CONFIG": str(config)}), \
                    patch.object(sys, "argv", ["entry", "--release", str(release), "stop"]), \
                    patch.object(entry, "profile", return_value=config), \
                    patch.object(entry, "dispatch", side_effect=AssertionError("capture")):
                with self.assertRaisesRegex(ValueError, "unsupported_development_replacement"):
                    entry.main()
