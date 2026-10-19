"""Focused checks for the packaging entry: bounded dependency probe before writes."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.process_fixtures import run_owned_fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import capture_config
import product_contract
import release_metadata
import setup as setup_script
import sharing


def _in_process_normalize(settings, python, config_file=None, *, previous=None):
    from mindie_knowledge.loop.settings import normalize, transition
    try:
        return normalize(settings) if previous is None else transition(previous, settings)
    except ValueError as exc:
        raise sharing.SharingError("community settings rejected by core normalize: " + str(exc)[:200])


def run_setup(python, *args, real_probe=False, real_store=False, env=None):
    # Config-write cases isolate the product verification boundary. Candidate
    # ownership, real subprocess failures and receipt identities have their
    # own tests; no config unit case downloads publication history.
    if real_probe:
        with tempfile.TemporaryDirectory() as watch:
            stage = Path(watch) / "stage"
            helper = (
                "import sys\nfrom pathlib import Path\n"
                "sys.path.insert(0,sys.argv.pop(1))\n"
                "stage=Path(sys.argv.pop(1));stage.write_text('import_setup')\n"
                "import setup\n"
                "stage.write_text('setup_main');setup.main()\n"
                "stage.write_text('completed')\n")
            return run_owned_fixture(
                [sys.executable, "-c", helper, str(SCRIPTS), str(stage),
                 "--knowledge-python", str(python), *map(str, args)],
                timeout=30, env=env,
                diagnose=lambda: dict(stage=stage.read_text() if stage.exists() else "python_startup",
                                      real_probe=real_probe, real_store=real_store),
            )
    argv = ["setup.py", "--knowledge-python", str(python), *map(str, args)]
    out, err = io.StringIO(), io.StringIO()
    code = 0

    def validate_store_config(_python, _scripts, config):
        from mindie_knowledge.loop.service_context import validate
        from mindie_knowledge.git_layout import validate_config_feed_layout
        validate(config)
        validate_config_feed_layout(config)

    with contextlib.ExitStack() as stack:
        if env is not None:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch.object(sys, "argv", argv))
        stack.enter_context(patch.object(release_metadata, "require_harness", lambda source: "0.153.4"))
        stack.enter_context(patch.object(
            setup_script, "probe_runtime",
            lambda _py: dict(product_contract.identity(product_contract.source_root(setup_script.SCRIPTS)), status="validated"),
        ))
        if not real_store:
            stack.enter_context(patch.object(capture_config, "prepare_store", validate_store_config))
        stack.enter_context(patch.object(sharing, "normalize_with_runtime", _in_process_normalize))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            setup_script.main()
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            else:
                if exc.code:
                    err.write(str(exc.code) + "\n")
                code = 1
        except Exception as exc:
            err.write(f"{type(exc).__name__}: {exc}\n")
            code = 1
    return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())


def run_configure(*args):
    argv = ["setup.py", "configure", *map(str, args)]
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with (patch.object(sys, "argv", argv),
          patch.object(sharing, "normalize_with_runtime", _in_process_normalize),
          contextlib.redirect_stdout(out),
          contextlib.redirect_stderr(err)):
        try:
            setup_script.main()
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            else:
                if exc.code:
                    err.write(str(exc.code) + "\n")
                code = 1
    return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())


class SetupTests(unittest.TestCase):
    def test_first_codex_install_reuses_unconfigured_disabled_shared_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "codex.json"
            consent = base / "mindie-consent.json"
            community = base / "mindie-community.json"
            community.write_text(json.dumps(dict(
                schema="mindie-community-config/1", enabled=False,
                generation=None, enabled_at=None, repository=None,
                branch="main", project_roots=[], idle_seconds=300,
                consent_config=str(consent),
            )))
            before = community.read_bytes()
            result = run_setup(sys.executable, "--config", config, "--root", base / "data")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(community.read_bytes(), before)
            self.assertFalse(consent.exists())
            receipt = json.loads(result.stdout)
            self.assertIsNone(receipt["reused_choice"])
            self.assertEqual(receipt["sharing"], "off")
            self.assertIn("off/unconfigured", receipt["next"])

    def test_first_codex_install_reuses_other_adapter_scope_and_consent_unchanged(self):
        from mindie_knowledge import consent_store
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                config = base / "codex.json"
                consent = base / "mindie-consent.json"
                community = base / "mindie-community.json"
                consent_store.record_choice(consent, "contribute" if enabled else "disabled")
                consent_store.record_reporting(consent, "disabled")
                community.write_text(json.dumps(dict(
                    schema="mindie-community-config/1", enabled=enabled,
                    generation="other-adapter", enabled_at=1.0, repository="owner/shared",
                    branch="main", project_roots=[str(base)], idle_seconds=300,
                    consent_config=str(consent), visibility="public",
                    sibling_extension={"preserve": True},
                )))
                before = consent.read_bytes(), community.read_bytes()
                result = run_setup(sys.executable, "--config", config, "--root", base / "data")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((consent.read_bytes(), community.read_bytes()), before)
                receipt = json.loads(result.stdout)
                self.assertEqual(receipt["reused_choice"], "contribute" if enabled else "disabled")
                self.assertEqual(receipt["sharing"], "enabled" if enabled else "off")
                self.assertIsNone(receipt["next"])
                self.assertEqual(json.loads(config.read_text())["community_config"], str(community))

    def test_first_install_preserves_invalid_shared_authority_before_store_or_config(self):
        from mindie_knowledge import consent_store
        for contents in ("{broken", json.dumps(dict(
            schema="mindie-community-config/1", enabled=False,
            project_roots=[], idle_seconds=300,
        ))):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                config = base / "codex.json"
                consent = base / "mindie-consent.json"
                community = base / "mindie-community.json"
                consent_store.record_choice(consent, "disabled")
                community.write_text(contents)
                before = consent.read_bytes(), community.read_bytes()
                result = run_setup(sys.executable, "--config", config, "--root", base / "data")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("existing shared community configuration", result.stderr)
                self.assertEqual((consent.read_bytes(), community.read_bytes()), before)
                self.assertFalse(config.exists())
                self.assertFalse((base / "data").exists())

    def test_invalid_service_environment_binding_fails_before_config_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "native-home"
            home.mkdir()
            (home / "config.toml").write_text(
                'model_provider="fixture"\n[model_providers.fixture]\n'
                'env_key="PYTHONPATH"\n', encoding="utf-8")
            config = base / "adapter.json"
            result = run_setup(
                sys.executable, "--config", config, "--root", base / "data",
                env=dict(os.environ, CODEX_HOME=str(home)),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("service_launch_context_invalid", result.stderr + result.stdout)
            self.assertFalse(config.exists())
            self.assertFalse((base / "adapter.engine.json").exists())

    def test_setup_saves_only_explicit_diagnostic_path_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            context = {
                "MINDIE_DIAGNOSTICS_CONFIG": str(base / "policy.json"),
                "MINDIE_DIAGNOSTICS_ROOT": str(base / "diagnostic-events"),
            }
            clean = {key: value for key, value in os.environ.items() if key not in context}
            clean["CODEX_HOME"] = str(base / "native-home")
            for explicit in (False, True):
                folder = base / str(explicit)
                config = folder / "codex.json"
                env = dict(clean, **context) if explicit else clean
                result = run_setup(sys.executable, "--config", config,
                                   "--root", folder / "data", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                adapter = json.loads(config.read_text())
                engine = json.loads(Path(adapter["engine_config"]).read_text())
                self.assertEqual(engine["service_launch_context"]["paths"]["MINDIE_AGENT_CONFIG"],
                                 str(config))
                self.assertEqual(engine["service_launch_context"]["paths"]["CODEX_HOME"],
                                 clean["CODEX_HOME"])
                self.assertEqual(adapter["native_context"]["CODEX_HOME"],
                                 str(Path(env.get("CODEX_HOME") or Path.home() / ".codex").absolute()))
                if explicit:
                    self.assertEqual(adapter["diagnostics_context"], context)
                else:
                    self.assertNotIn("diagnostics_context", adapter)
            self.assertFalse((base / "policy.json").exists())
            self.assertFalse((base / "diagnostic-events").exists())

    def test_missing_dependencies_fail_clearly_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Isolate the missing-dependency environment from the test runner.
            bare = base / "bare-runtime"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(bare)],
                check=True,
                timeout=30,
                capture_output=True,
            )
            python = bare / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            config = base / "codex.json"
            result = run_setup(python, "--config", config, "--root", base / "data", real_probe=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("knowledge runtime probe failed", result.stderr + result.stdout)
            self.assertIn("not retried", result.stderr + result.stdout)
            self.assertIn("runtime_pins: package_missing", result.stderr + result.stdout)
            # Nothing may be written when the probe fails.
            self.assertEqual(list(base.iterdir()), [bare])

    def test_probe_includes_runtime_packages(self):
        self.assertEqual(
            set(setup_script.PROBE_MODULES),
            {
                "mindie_knowledge.loop.cli",
                "mindie_knowledge.loop.documents",
                "mindie_knowledge.loop.activation",
                "mindie_knowledge.materials.reme_index",
                "langmem.short_term",
                "remote_dev.mcp.server",
            },
        )
        self.assertFalse(hasattr(setup_script, 'PROBE_TIMEOUT'))

    def test_corrupt_existing_store_blocks_config_publication_and_preserves_state(self):
        from mindie_knowledge.loop.store import Store
        from mindie_knowledge.state_layout import state_root
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = Store(base / 'data', 'vllm-ascend')
            store.close()
            state = state_root(base / 'data', 'vllm-ascend')
            database = state / 'state-v4.sqlite3'
            database.write_bytes(b'corrupt authority sentinel')
            before = {str(path.relative_to(base)): path.read_bytes()
                      for path in base.rglob('*') if path.is_file() and path.suffix != '.lock'}
            config = base / 'codex.json'
            result = run_setup(sys.executable, '--config', config, '--root', base / 'data', real_store=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('knowledge store preparation failed', result.stdout + result.stderr)
            self.assertIn('database', result.stdout + result.stderr)
            self.assertFalse(config.exists())
            self.assertFalse(config.with_name('codex.engine.json').exists())
            after = {str(path.relative_to(base)): path.read_bytes()
                     for path in base.rglob('*') if path.is_file() and path.suffix != '.lock'}
            self.assertEqual(after, before)

    def test_complete_runtime_writes_private_config_and_refuses_overwrite(self):
        for module in setup_script.PROBE_MODULES:
            try:
                __import__(module)
            except ImportError:
                self.fail(f"pinned runtime not installed in {sys.executable}; run tests/preflight.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "codex.json"
            result = run_setup(sys.executable, "--config", config, "--root", base / "data", real_store=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            engine = config.with_name("codex.engine.json")
            if os.name == "posix":
                self.assertEqual((config.stat().st_mode & 0o777), 0o600)
                self.assertEqual((engine.stat().st_mode & 0o777), 0o600)
            else:
                # Windows mode bits do not describe the inherited profile
                # ACL; file ACL review remains part of native acceptance.
                self.assertTrue(config.is_file())
                self.assertTrue(engine.is_file())
            value = json.loads(engine.read_text())
            # Installation prepares the real state needed by the first Stop,
            # without query, activation, feed sync or a running service.
            from mindie_knowledge.state_layout import state_root
            state = state_root(base / 'data', 'vllm-ascend')
            self.assertTrue((state / 'state-v4.sqlite3').is_file())
            self.assertFalse((base / 'data/vllm-ascend/connection.json').exists())
            adapter = json.loads(config.read_text())
            self.assertEqual(value["domain"], "vllm-ascend")
            self.assertEqual(value["capture_mode"], "public-transcript")
            self.assertNotIn("agent_command", value)
            self.assertTrue(Path(value["redactor_executable"]).is_file())
            self.assertEqual(value["summary_command"], [sys.executable, str(SCRIPTS / "agent_worker.py")])
            self.assertNotIn("session_activation", value)
            self.assertNotIn("session_activation", adapter)
            admission = config.with_name("codex.admission.sqlite3")
            self.assertEqual(value["admission_path"], str(admission))
            self.assertEqual(adapter["admission_path"], str(admission))
            self.assertTrue(Path(value["transcript_adapter"]).is_absolute())
            self.assertTrue(value["transcript_adapter"].endswith("codex_transcript.py"))
            self.assertEqual(adapter["runtime_scripts"], str(SCRIPTS))
            declaration, _ = setup_script.product_contract.product(ROOT)
            self.assertEqual(value["feeds"], [setup_script.product_contract.publication_feed(declaration)])
            self.assertEqual(adapter["product_validation"], value["product_validation"])
            self.assertEqual(adapter["product_validation"]["publication"], declaration["publication"])
            self.assertNotIn("sharing_choice", adapter)
            # Community sharing defaults OFF: the pointer exists, the file not.
            community = config.with_name("mindie-community.json")
            self.assertEqual(value["community_config"], str(community))
            self.assertFalse(community.exists())
            self.assertFalse(admission.exists())
            again = run_setup(sys.executable, "--config", config, "--root", base / "data")
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("configuration already exists", again.stderr)
            # Sharing changes do not rewrite either installation binding.
            installed_bytes = config.read_bytes(), engine.read_bytes()
            scope = base / "scope"
            scope.mkdir()
            configured = run_configure(
                "--config", str(config),
                "--community-repository", "mindie-agent/knowledge",
                "--community-project-root", str(scope),
                "--community-visibility", "public",
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)
            settings = json.loads(community.read_text())
            self.assertTrue(settings["enabled"])
            # The explicit choice lands in the consent authority; the retired
            # adapter-config key is gone and the gate pointer is wired.
            adapter = json.loads(config.read_text())
            self.assertNotIn("sharing_choice", adapter)
            consent_doc = json.loads(
                config.with_name("mindie-consent.json").read_text()
            )
            self.assertEqual(consent_doc["choice"], "contribute")
            self.assertEqual(
                settings["consent_config"],
                str(config.with_name("mindie-consent.json")),
            )
            self.assertEqual((config.read_bytes(), engine.read_bytes()), installed_bytes)

    def test_configure_rejects_corrupt_or_development_engine_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "codex.json"
            installed = run_setup(sys.executable, "--config", config, "--root", base / "data")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            engine = config.with_name("codex.engine.json")
            original = json.loads(engine.read_text())
            scope = base / "scope"
            scope.mkdir()
            for invalid in (b"{broken", json.dumps(dict(original, session_activation=str(config))).encode()):
                with self.subTest(invalid=invalid[:20]):
                    engine.write_bytes(invalid)
                    before = {path.name: path.read_bytes() for path in base.iterdir() if path.is_file()}
                    result = run_configure(
                        "--config", str(config), "--community-repository", "owner/explicit",
                        "--community-project-root", str(scope), "--community-visibility", "public",
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("engine configuration is unavailable or unsupported", result.stderr)
                    self.assertEqual(
                        {path.name: path.read_bytes() for path in base.iterdir() if path.is_file()}, before)

    def test_configure_preserves_a_damaged_community_document(self):
        for module in setup_script.PROBE_MODULES:
            try:
                __import__(module)
            except ImportError:
                self.skipTest(f"pinned runtime not installed in {sys.executable}")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "codex.json"
            result = run_setup(sys.executable, "--config", config, "--root", base / "data")
            self.assertEqual(result.returncode, 0, result.stderr)
            scope = base / "scope"
            scope.mkdir()
            community = config.with_name("mindie-community.json")
            community.write_text("{broken-community")
            before = community.read_bytes()
            configured = run_configure(
                "--config", str(config),
                "--community-repository", "owner/explicit",
                "--community-project-root", str(scope),
                "--community-visibility", "public",
            )
            self.assertNotEqual(configured.returncode, 0)
            self.assertIn("damaged", configured.stderr)
            self.assertEqual(community.read_bytes(), before)
            # A parseable document with malformed managed values is the
            # explicit repair path: configure rewrites it normally.
            community.write_text(json.dumps(
                dict(schema="mindie-community-config/1", enabled="yes",
                     project_roots="not-a-list", idle_seconds=300,
                     sibling_key={"owned": "extension"})
            ))
            repaired = run_configure(
                "--config", str(config),
                "--community-repository", "owner/explicit",
                "--community-project-root", str(scope),
                "--community-visibility", "public",
            )
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            settings = json.loads(community.read_text())
            self.assertTrue(settings["enabled"])
            self.assertEqual(settings["repository"], "owner/explicit")
            self.assertEqual(settings["sibling_key"], {"owned": "extension"})

    def test_community_selection_records_settings_and_enables_sharing(self):
        for module in setup_script.PROBE_MODULES:
            try:
                __import__(module)
            except ImportError:
                self.skipTest(f"pinned runtime not installed in {sys.executable}")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scope = base / "scope"
            scope.mkdir()
            config = base / "codex.json"
            result = run_setup(
                sys.executable,
                "--config",
                config,
                "--root",
                base / "data",
                "--community-repository",
                "mindie-agent/knowledge-vllm-ascend",
                "--community-project-root",
                str(scope),
                "--community-account",
                "contributor-1",
                "--community-visibility",
                "public",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            community = config.with_name("mindie-community.json")
            if os.name == "posix":
                self.assertEqual((community.stat().st_mode & 0o777), 0o600)
            else:
                # Windows inherits the temporary profile directory ACL; this
                # test does not claim that the ACL is restrictive.
                self.assertTrue(community.is_file())
            settings = json.loads(community.read_text())
            self.assertEqual(settings["schema"], "mindie-community-config/1")
            declaration, _ = setup_script.product_contract.product(ROOT)
            self.assertEqual(settings["publication_contract_sha256"], declaration["publication"]["contract_sha256"])
            self.assertTrue(settings["enabled"])
            self.assertEqual(settings["repository"], "mindie-agent/knowledge-vllm-ascend")
            self.assertEqual(settings["project_roots"], [str(scope.resolve())])
            self.assertEqual(settings["account"], "contributor-1")
            self.assertEqual(settings["visibility"], "public")
            self.assertGreater(settings["enabled_at"], 0)
            # The consent authority pointer is wired at install.
            self.assertEqual(
                settings["consent_config"],
                str(config.with_name("mindie-consent.json")),
            )
            consent_doc = json.loads(
                config.with_name("mindie-consent.json").read_text()
            )
            self.assertEqual(consent_doc["choice"], "contribute")
            self.assertNotIn("token", community.read_text().lower())

    def test_first_configure_projects_product_and_repo_change_drops_old_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "codex.json"
            scope = base / "scope"
            scope.mkdir()
            installed = run_setup(sys.executable, "--config", config, "--root", base / "data")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            declaration, _ = setup_script.product_contract.product(ROOT)
            def configure(repository):
                result = run_configure(
                    "--config", str(config), "--community-repository", repository,
                    "--community-project-root", str(scope),
                    "--community-visibility", "public",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(config.with_name("mindie-community.json").read_text())
            selected = configure(declaration["publication"]["repository"])
            self.assertEqual(selected["publication_contract_sha256"], declaration["publication"]["contract_sha256"])
            selected = configure("owner/custom")
            self.assertNotIn("publication_contract_sha256", selected)
            selected["publication_contract_sha256"] = "b" * 64
            config.with_name("mindie-community.json").write_text(json.dumps(selected))
            self.assertEqual(configure("owner/custom")["publication_contract_sha256"], "b" * 64)

    def test_partial_community_selection_fails_before_any_write(self):
        for module in setup_script.PROBE_MODULES:
            try:
                __import__(module)
            except ImportError:
                self.skipTest(f"pinned runtime not installed in {sys.executable}")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "codex.json"
            # Repository without scope/visibility is refused.
            result = run_setup(
                sys.executable,
                "--config",
                config,
                "--root",
                base / "data",
                "--community-repository",
                "mindie-agent/knowledge",
            )
            self.assertNotEqual(result.returncode, 0)
            # A missing project root directory is refused as well.
            result = run_setup(
                sys.executable,
                "--config",
                config,
                "--root",
                base / "data",
                "--community-repository",
                "mindie-agent/knowledge",
                "--community-project-root",
                str(base / "missing"),
                "--community-visibility",
                "public",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
