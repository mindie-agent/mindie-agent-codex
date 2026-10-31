"""Compatibility selection and native effects remain separate from activation."""
import json
import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import harness_update as update
import native_hooks
import release_metadata
import update_trigger
from bounded_process import ProcessResult, ProcessCleanupError
from update_lock import file_lock


class UpdateTests(unittest.TestCase):
    def test_owned_update_fault_is_recorded_and_ingested_with_failed_receipt(self):
        from mindie_diagnostics.integration import configure_reporting
        from mindie_diagnostics.ingestion import ingest
        from mindie_diagnostics.outbox import Outbox
        policy = Path(os.environ['MINDIE_DIAGNOSTICS_CONFIG'])
        logs = Path(os.environ['MINDIE_DIAGNOSTICS_ROOT'])
        configure_reporting(True, config=policy, roots=[str(logs)])
        self.installed.side_effect = ModuleNotFoundError('PRIVATE-MODULE')
        result = update.check(self.config)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['diagnostic']['recorded'])
        self.assertEqual(json.loads((self.state / 'status.json').read_text())['diagnostic'], result['diagnostic'])
        queue = Outbox(self.root / 'queue.db')
        self.assertEqual(ingest(logs, queue, config=policy)['enqueued'], 1)
        self.assertEqual(queue.rows()[0]['incident_ids'], [result['diagnostic']['incident_id']])

    def test_helper_dies_before_status_receipt_still_produces_diagnostic(self):
        from bounded_process import run
        try:
            run([sys.executable, '-I', '-c', 'import missing_mindie_helper_dependency'], '', timeout=5)
        except RuntimeError as error:
            primary = error
        self.config.write_text(json.dumps({'python': sys.executable}))
        maintenance = update_trigger.Maintenance(self.config)
        with patch.object(update_trigger, 'runtime_scripts', return_value=str(SCRIPTS)), \
                patch.object(update_trigger, 'runtime_env', return_value={}), \
                patch.object(update_trigger, 'run', side_effect=primary):
            maintenance._run()
        self.assertTrue(maintenance.last_failure['diagnostic']['recorded'])
        self.assertEqual(maintenance.last_failure['status'], 'failed')
        self.assertGreater(maintenance.next_attempt, time.time())

    def test_component_inheritance_crash_keeps_one_incident_through_outer_boundary(self):
        import bootstrap
        import diagnostic_support
        active, helper = self.root / 'active', self.root / 'helper'
        active.mkdir()
        helper.mkdir()
        (active / 'runtime.json').write_text(json.dumps({'validation': {'source_sha256': 'old'}}))
        # Unmodified entry point with a missing committed dependency: the real
        # isolated child fails before it can emit a structured response.
        (helper / 'component_update.py').write_bytes((SCRIPTS / 'component_update.py').read_bytes())
        self.config.write_text(json.dumps({'component_release': str(active), 'python': sys.executable}))
        with patch('session_gate.config_path', return_value=self.config), \
                patch.object(bootstrap, 'source_identity', return_value='new'), \
                patch.object(bootstrap, '_baseline_runtime', return_value=(Path(sys.executable), helper, active)):
            with self.assertRaises(RuntimeError) as caught:
                with bootstrap.preparing('outer'):
                    bootstrap.ensure_runtime(SCRIPTS.parent, self.root)
        ref = caught.exception.mindie_diagnostic
        self.assertTrue(ref['recorded'])
        outer = diagnostic_support.failure('release.update', 'activation', 'update', exception=caught.exception)
        self.assertEqual(outer['incident_id'], ref['incident_id'])
        self.assertFalse(outer['logging_failed'])
        root = Path(os.environ['MINDIE_DIAGNOSTICS_ROOT'])
        events = [json.loads(line) for path in root.glob('events/*/*.jsonl')
                  for line in path.read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['attributes']['stage'], 'component-inheritance')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.json"
        self.env = dict(CODEX_HOME=str(self.root / "native"),
                        MINDIE_CODEX_BIN=str(self.root / "fixture" / "codex"))
        self.config.write_text(json.dumps(dict(native_context=self.env)))
        self.metadata = release_metadata.metadata(SCRIPTS.parent, "a" * 40)
        self.version = self.metadata["version"]
        self.release = dict(id=1, tag_name=f"v{self.version}", draft=False, prerelease=False,
                            metadata=self.metadata)
        self.state = update_trigger.directory(self.config)
        self.state.mkdir()
        self.before = dict(version="0.0.0", enabled=True,
                           marketplaceSource=dict(sourceType="local",
                                                  source=str(self.state / "catalog")))
        self.patch("harness_version", "0.153.4")
        self.patch("runtime_env", self.env)
        self.installed = self.patch("installed", self.before)
        component = patch("component_update.check", return_value={"status": "up_to_date"})
        self.components = component.start()
        self.addCleanup(component.stop)

    def patch(self, name, result):
        context = patch.object(update, name, return_value=result)
        value = context.start()
        self.addCleanup(context.stop)
        return value

    def test_native_version_strict_readback(self):
        # Test the actual function, independent of the check-level version fixture.
        from importlib import util
        spec = util.spec_from_file_location("native_update_version_fixture", SCRIPTS / "harness_update.py")
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for raw, expected in (("codex-cli 0.153.4\n", "0.153.4"),
                              ("codex-cli 0.162.0-alpha.17.2\n", "0.162.0-alpha.17.2")):
            with patch.object(module, "run", return_value=ProcessResult("completed", raw, 0)):
                self.assertEqual(module.harness_version("/native", {}), expected)
        with patch.object(module, "run", return_value=ProcessResult("completed", "unknown\n", 0)):
            with self.assertRaises(ValueError):
                module.harness_version("/native", {})

    def test_catalog_contains_only_selected_candidate(self):
        value = update.catalog(self.metadata)
        self.assertEqual(len(value["plugins"]), 1)
        source = value["plugins"][0]["source"]
        self.assertEqual(source["source"], "git-subdir")
        self.assertEqual(source["sha"], "a" * 40)
        self.assertEqual(source["path"], "plugins/mindie-agent")
        self.assertNotIn("ref", source)

    def test_native_install_once_and_readback(self):
        source = Path(self.env["CODEX_HOME"]) / "plugins/cache/mindie-agent/mindie-agent" / self.version
        (source / ".codex-plugin").mkdir(parents=True)
        (source / ".codex-plugin/plugin.json").write_text(json.dumps(dict(version=self.version)))
        (source / "pyproject.toml").write_text(f'[project]\nversion="{self.version}"\n')
        (source / "harness-compatibility.json").write_text(json.dumps(self.metadata["compatibility"]))
        self.installed.return_value = dict(self.before, version=self.version)
        output = dict(pluginId=update.PLUGIN, version=self.version, installedPath=str(source))
        with patch.object(update, "run", return_value=ProcessResult(
                "completed", json.dumps(output), 0)) as native, \
                patch.object(native_hooks, "snapshot", return_value=[]):
            pending = update._native_install("/native", self.env, self.state,
                                              self.metadata, self.before)
        native.assert_called_once()
        self.assertEqual(native.call_args.args[0][1:3], ["plugin", "add"])
        self.assertEqual(pending["stage"], "installed")
        self.assertEqual(pending["process"],
                         dict(execution="completed", returncode=0, cleanup_failed=False))

    def test_unknown_native_matching_target_never_repeats_or_activates(self):
        pending = dict(metadata=self.metadata, stage="plugin_add",
                       process=dict(execution="unknown", returncode=None, cleanup_failed=False))
        (self.state / "native.json").write_text(json.dumps(pending))
        self.installed.return_value = dict(self.before, version=self.version)
        with patch.object(update, "_native_install") as install, \
                patch.object(update, "_prepare_and_activate") as activate:
            result = update.check(self.config)
        self.assertEqual(result["status"], "unknown")
        install.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(json.loads((self.state / "native.json").read_text()), pending)

    def test_cleanup_failure_even_matching_target_stays_unknown(self):
        (self.state / "native.json").write_text(json.dumps(dict(
            metadata=self.metadata, stage="plugin_add",
            process=dict(execution="completed", returncode=0, cleanup_failed=True))))
        self.installed.return_value = dict(self.before, version=self.version)
        with patch.object(update, "_prepare_and_activate") as activate:
            result = update.check(self.config)
        self.assertEqual(result["status"], "unknown")
        activate.assert_not_called()

    def test_native_parse_failure_preserves_completed_writer_fact(self):
        pending = dict(metadata=self.metadata)
        with patch.object(update, "run", return_value=ProcessResult(
                "completed", "invalid native JSON", 0)):
            with self.assertRaises(json.JSONDecodeError) as caught:
                update._native_mutation(self.state / "native.json", pending,
                                        "plugin_add", ["/native"], {})
        fact = dict(execution="completed", returncode=0, cleanup_failed=False)
        self.assertEqual(caught.exception.native_effect["process"], fact)
        self.assertEqual(json.loads((self.state / "native.json").read_text())["process"], fact)
        self.assertNotIn("native_output", pending)

    def test_native_error_and_failed_receipt_keep_original_outcome(self):
        primary = RuntimeError("native failed without disclosing private output")
        primary.process_result = ProcessResult("unknown", "", None)
        with patch.object(update, "_atomic", side_effect=[None, OSError("private path")]), \
                patch.object(update, "run", side_effect=primary):
            with self.assertRaises(RuntimeError) as caught:
                update._native_mutation(self.state / "native.json", {},
                                        "plugin_add", ["/native"], {})
        self.assertIs(caught.exception, primary)
        self.assertEqual(primary.native_effect["process"]["execution"], "unknown")
        self.assertEqual(primary.receipt_error_type, "OSError")

    def test_successful_native_effect_survives_receipt_write_failure(self):
        with patch.object(update, "_atomic", side_effect=[None, OSError("private path")]), \
                patch.object(update, "run", return_value=ProcessResult("completed", "{}", 0)):
            with self.assertRaises(OSError) as caught:
                update._native_mutation(self.state / "native.json", {},
                                        "plugin_add", ["/native"], {})
        self.assertEqual(caught.exception.native_effect["process"],
                         dict(execution="completed", returncode=0, cleanup_failed=False))

    def test_catalog_retry_clears_previous_settled_writer_before_new_attempt(self):
        pending = dict(metadata=self.metadata, stage="catalog_select",
                       writer_settled=True,
                       process=dict(execution="completed", returncode=1, cleanup_failed=False))
        with patch.object(native_hooks, "NativeHooks") as api:
            api.return_value.__enter__.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                update._select_catalog("/native", self.env, self.state, pending)
        saved = json.loads((self.state / "native.json").read_text())
        self.assertIs(saved["writer_settled"], False)
        self.assertIsNone(saved["process"])
        with patch.object(native_hooks, "NativeHooks") as api, \
                patch.object(update, "_native_install") as install, \
                patch.object(update, "_prepare_and_activate") as activate:
            result = update.check(self.config)
        self.assertEqual(result["status"], "unknown")
        api.assert_not_called()
        install.assert_not_called()
        activate.assert_not_called()

    def test_runtime_cleanup_failure_cannot_start_another_preparation_or_native_write(self):
        source = self.root / "source"
        (source / ".codex-plugin").mkdir(parents=True)
        (source / ".codex-plugin/plugin.json").write_text(json.dumps(dict(version=self.version)))
        (source / "pyproject.toml").write_text(f'[project]\nversion="{self.version}"\n')
        (source / "harness-compatibility.json").write_text(json.dumps(self.metadata["compatibility"]))
        pending = dict(metadata=self.metadata, stage="installed", source=str(source),
                       process=dict(execution="completed", returncode=0, cleanup_failed=False))
        result = ProcessResult("unknown", cleanup=[dict(stage="terminate_owned_tree",
                                                       error_type="OSError")])
        with patch.object(update, "run", side_effect=ProcessCleanupError(result)):
            with self.assertRaises(ProcessCleanupError):
                update._prepare_and_activate(self.config, pending, self.env, self.state)
        self.assertEqual(pending["stage"], "runtime_prepare_unknown")
        self.assertFalse(update._can_supersede(pending))
        with patch.object(update, "_native_install") as install, \
                patch.object(update, "_prepare_and_activate") as prepare:
            status = update.check(self.config)
        self.assertEqual(status["status"], "unknown")
        install.assert_not_called()
        prepare.assert_not_called()

    def test_interrupted_runtime_prepare_without_cleanup_failure_retries_preparation(self):
        source = self.root / "source"
        (source / ".codex-plugin").mkdir(parents=True)
        (source / ".codex-plugin/plugin.json").write_text(json.dumps(dict(version=self.version)))
        (source / "pyproject.toml").write_text(f'[project]\nversion="{self.version}"\n')
        (source / "harness-compatibility.json").write_text(json.dumps(self.metadata["compatibility"]))
        self.installed.return_value = dict(self.before, version=self.version)
        for prep in (
            dict(execution="unknown", cleanup_failed=None),
            dict(execution="unknown", returncode=None, cleanup_failed=False),
        ):
            pending = dict(metadata=self.metadata, stage="installed", source=str(source),
                           process=dict(execution="completed", returncode=0, cleanup_failed=False),
                           preparation=prep)
            (self.state / "native.json").write_text(json.dumps(pending))
            with patch.object(update, "_native_install") as install, \
                    patch.object(update, "_prepare_and_activate",
                                 return_value=dict(status="active")) as prepare:
                status = update.check(self.config)
            self.assertEqual(status["status"], "active")
            install.assert_not_called()
            prepare.assert_called_once()
            (self.state / "status.json").unlink(missing_ok=True)

    def prepared_candidate(self):
        source = self.root / "source"
        (source / ".codex-plugin").mkdir(parents=True)
        (source / ".codex-plugin/plugin.json").write_text(json.dumps(dict(version=self.version)))
        (source / "pyproject.toml").write_text(f'[project]\nversion="{self.version}"\n')
        (source / "harness-compatibility.json").write_text(json.dumps(self.metadata["compatibility"]))
        return dict(metadata=self.metadata, stage="prepared", source=str(source),
                    runtime=dict(python="/candidate/python", scripts="/candidate/scripts",
                                 release="/candidate/release"),
                    hook_trust=dict(status="complete"),
                    activation=dict(status="not_started", expected_binding=update.runtime_binding(
                        json.loads(self.config.read_text()))))

    def test_activation_unknown_writer_retries_with_immutable_attempt_snapshot(self):
        pending = self.prepared_candidate()
        pending["activation"].update(status="unknown", process=None)
        seen = []

        def child(argv, payload, **kwargs):
            seen.append(json.loads(payload))
            return ProcessResult("completed", '{"status":"deferred","reason":"operation_busy"}', 0)

        with patch.object(update, "run", side_effect=child) as run:
            value = update._prepare_and_activate(self.config, pending, self.env, self.state)
        self.assertEqual(value["status"], "deferred")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(seen[0]["schema"], "mindie-activation-attempt/1")
        self.assertIn("expected_binding", seen[0])
        saved = json.loads((self.state / "native.json").read_text())
        self.assertEqual(saved["activation"]["status"], "deferred")
        self.assertIn("expected_binding", saved["activation"])

    def test_prepared_runtime_recovery_cannot_replace_newer_components_of_the_same_plugin(self):
        original = dict(
            python=sys.executable, runtime_scripts=str(SCRIPTS),
            engine_config=str(self.root / "old.engine.json"),
            product_validation=dict(native_version=self.version,
                                    runtime={"mindie-knowledge": "a" * 40}),
        )
        self.config.write_text(json.dumps(original))
        expected = update.runtime_binding(original)
        pending = self.prepared_candidate()
        for field in ("runtime", "activation", "hook_trust"):
            pending.pop(field)
        pending["stage"] = "installed"
        release = self.root / "candidate-release"
        release.mkdir()
        (release / "runtime.json").write_text(json.dumps(dict(
            python=sys.executable, validation=original["product_validation"])))
        runtime = dict(python=sys.executable, scripts=str(SCRIPTS), release=str(release))

        def prepare(argv, payload, **kwargs):
            self.assertEqual(argv[0], "uv")
            saved = json.loads((self.state / "native.json").read_text())
            self.assertEqual(saved["activation"]["expected_binding"], expected)
            return ProcessResult("completed", json.dumps(runtime), 0)

        with patch.object(update, "run", side_effect=prepare), \
                patch.object(native_hooks, "preserve", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                update._prepare_and_activate(self.config, pending, self.env, self.state)
        recovered = json.loads((self.state / "native.json").read_text())
        self.assertEqual(recovered["stage"], "prepared")
        self.assertEqual(recovered["activation"]["expected_binding"], expected)
        advanced = dict(original, product_validation=dict(
            native_version=self.version, runtime={"mindie-knowledge": "b" * 40}))
        self.config.write_text(json.dumps(advanced))
        before = self.config.read_bytes()
        real_run = update.run

        def activate(argv, payload, **kwargs):
            self.assertEqual(Path(argv[2]).name, "release_activation.py")
            self.assertEqual(json.loads(payload)["expected_binding"], expected)
            return real_run(argv, payload, **kwargs)

        with patch.object(update, "run", side_effect=activate), \
                patch.object(native_hooks, "preserve", return_value={"status": "complete"}):
            result = update._prepare_and_activate(self.config, recovered, self.env, self.state)
        self.assertEqual(result["reason"], "installation_changed")
        self.assertEqual(result["execution"], "not_started")
        self.assertEqual(self.config.read_bytes(), before)

    def test_legacy_local_preparation_is_rebuilt_before_adopting_a_current_binding(self):
        pending = self.prepared_candidate()
        pending["activation"] = {"status": "not_started"}
        rebuilt = dict(python=sys.executable, scripts=str(SCRIPTS),
                       release=str(self.root / "rebuilt-release"))
        expected = update.runtime_binding(json.loads(self.config.read_text()))
        seen = []

        def child(argv, payload, **kwargs):
            seen.append(argv)
            if len(seen) == 1:
                self.assertEqual(argv[0], "uv")
                saved = json.loads((self.state / "native.json").read_text())
                self.assertNotIn("runtime", saved)
                self.assertEqual(saved["activation"]["expected_binding"], expected)
                return ProcessResult("completed", json.dumps(rebuilt), 0)
            self.assertEqual(argv[-2:], [rebuilt["release"], "--attempt"])
            self.assertEqual(json.loads(payload)["expected_binding"], expected)
            return ProcessResult("completed", '{"status":"deferred","reason":"operation_busy"}', 0)

        with patch.object(update, "run", side_effect=child):
            update._prepare_and_activate(self.config, pending, self.env, self.state)
        self.assertEqual(len(seen), 2)
        self.assertEqual(pending["runtime"], rebuilt)

    def test_running_and_completed_activation_recovery_keep_the_preparation_snapshot(self):
        pending = self.prepared_candidate()
        expected = pending["activation"]["expected_binding"]
        self.config.write_text(json.dumps(dict(
            product_validation={"native_version": self.version,
                                "runtime": {"mindie-knowledge": "b" * 40}})))
        for status, process in (
            ("unconfirmed", None),
            ("active", dict(execution="completed", returncode=0, cleanup_failed=False)),
        ):
            with self.subTest(status=status):
                recovered = copy.deepcopy(pending)
                recovered["activation"].update(status=status, process=process)

                def child(argv, payload, **kwargs):
                    self.assertEqual(Path(argv[2]).name, "release_activation.py")
                    self.assertEqual(json.loads(payload)["expected_binding"], expected)
                    return ProcessResult("completed", '{"status":"deferred","reason":"installation_changed"}', 0)

                with patch.object(update, "run", side_effect=child) as run:
                    update._prepare_and_activate(self.config, recovered, self.env, self.state)
                run.assert_called_once()
                saved = json.loads((self.state / "native.json").read_text())
                self.assertEqual(saved["activation"]["expected_binding"], expected)

    def test_busy_old_update_lock_defers_without_spawning_or_unknown(self):
        pending = self.prepared_candidate()
        with file_lock(self.config.with_suffix(".update.lock")):
            with patch.object(update, "run") as run:
                value = update._prepare_and_activate(
                    self.config, pending, self.env, self.state)
        self.assertEqual(value["status"], "deferred")
        self.assertEqual(value["reason"], "operation_busy")
        run.assert_not_called()
        saved = json.loads((self.state / "native.json").read_text())
        self.assertEqual(saved["activation"]["status"], "unconfirmed")
        self.assertIn("expected_binding", saved["activation"])

    def test_legacy_candidate_activation_version_floor_never_invokes_helper(self):
        pending = self.prepared_candidate()
        pending["metadata"]["version"] = "0.1.9"
        with patch.object(update, "native_version", return_value="0.1.9"), \
                patch.object(update, "run") as run:
            value = update._prepare_and_activate(
                self.config, pending, self.env, self.state)
        self.assertEqual(value["status"], "deferred")
        self.assertEqual(value["reason"], "legacy_activation_helper")
        run.assert_not_called()
        self.assertFalse((self.state / "native.json").exists())

    def test_activation_cleanup_failure_is_retried_not_a_permanent_prison(self):
        pending = self.prepared_candidate()
        pending["activation"].update(
            status="unknown",
            process=dict(execution="unknown", returncode=None, cleanup_failed=True))
        with patch.object(update, "run", return_value=ProcessResult(
                "completed", '{"status":"active"}', 0)) as run:
            value = update._prepare_and_activate(self.config, pending, self.env, self.state)
        self.assertEqual(value["status"], "active")
        run.assert_called_once()

    def test_activation_result_and_cleanup_failure_are_both_retained(self):
        pending = self.prepared_candidate()
        result = ProcessResult("completed", '{"status":"active"}', 0,
                               cleanup=[dict(stage="terminate_owned_tree", error_type="OSError")])
        with patch.object(update, "run", return_value=result):
            with self.assertRaises(ProcessCleanupError) as caught:
                update._prepare_and_activate(self.config, pending, self.env, self.state)
        saved = json.loads((self.state / "native.json").read_text())
        self.assertEqual(saved["activation"]["status"], "active")
        self.assertIs(saved["activation"]["process"]["cleanup_failed"], True)
        self.assertEqual(caught.exception.native_effect["activation"], saved["activation"])
        with patch.object(update, "run", return_value=ProcessResult(
                "completed", '{"status":"active"}', 0)) as run:
            value = update._prepare_and_activate(self.config, saved, self.env, self.state)
        self.assertEqual(value["status"], "active")
        run.assert_called_once()

    def test_settled_activation_reconciliation_preserves_result_if_recording_fails(self):
        pending = self.prepared_candidate()
        pending["activation"].update(status="unknown", process=dict(
            execution="unknown", returncode=-9, cleanup_failed=False))
        with patch.object(update, "run", return_value=ProcessResult(
                "completed", '{"status":"active"}', 0)) as run, \
                patch.object(update, "_atomic", side_effect=[None, OSError("private path")]):
            with self.assertRaises(OSError) as caught:
                update._prepare_and_activate(self.config, pending, self.env, self.state)
        run.assert_called_once()
        expected_binding = dict(python=None, runtime_scripts=None, engine_config=None,
                                product_validation=None, component_commands={})
        self.assertEqual(caught.exception.native_effect["activation"], dict(
            status="active", process=dict(execution="completed", returncode=0, cleanup_failed=False),
            expected_binding=expected_binding))
        self.assertEqual(caught.exception.receipt_error_type, "OSError")

    def next_patch_version(self):
        major, minor, patch = self.version.split(".")
        return f"{major}.{minor}.{int(patch) + 1}"

    def stale_candidate(self):
        pending = self.prepared_candidate()
        pending.update(
            process=dict(execution="completed", returncode=0, cleanup_failed=False),
            preparation=dict(execution="completed", returncode=0, cleanup_failed=False),
            native_output=dict(pluginId=update.PLUGIN, version=self.version))
        return pending

    def test_stale_prepared_candidate_is_retired_before_compatibility_or_selection(self):
        pending = self.stale_candidate()
        (self.state / "native.json").write_text(json.dumps(pending))
        self.installed.return_value = dict(self.before, version=self.next_patch_version())
        with patch("mindie_knowledge.releases_io.fetch_releases", return_value=[]) as fetch, \
                patch.object(update, "_native_install") as install:
            value = update.check(self.config)
        self.assertEqual(value["status"], "no_compatible_release")
        fetch.assert_called_once()
        install.assert_not_called()
        archived = list(self.state.glob("superseded-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text()), pending)
        self.assertEqual(json.loads((self.state / "native.json").read_text())["stage"], "superseded")

    def test_stale_prepared_candidate_with_evicted_native_source_is_retired(self):
        pending = self.stale_candidate()
        Path(pending["source"]).rename(self.root / "evicted-source")
        self.assertFalse(Path(pending["source"]).exists())
        (self.state / "native.json").write_text(json.dumps(pending))
        self.installed.return_value = dict(self.before, version=self.next_patch_version())
        with patch("mindie_knowledge.releases_io.fetch_releases", return_value=[]) as fetch, \
                patch.object(update, "_native_install") as install:
            value = update.check(self.config)
        self.assertEqual(value["status"], "no_compatible_release")
        fetch.assert_called_once()
        install.assert_not_called()
        archived = list(self.state.glob("superseded-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text()), pending)
        self.assertEqual(json.loads((self.state / "native.json").read_text())["stage"], "superseded")

    def test_stale_installed_candidate_with_evicted_source_is_not_retired(self):
        pending = dict(self.stale_candidate(), stage="installed")
        Path(pending["source"]).rename(self.root / "evicted-installed-source")
        (self.state / "native.json").write_text(json.dumps(pending))
        self.installed.return_value = dict(self.before, version=self.next_patch_version())
        with patch("mindie_knowledge.releases_io.fetch_releases") as fetch, \
                patch.object(update, "_native_install") as install:
            value = update.check(self.config)
        self.assertEqual(value["status"], "unknown")
        self.assertEqual(value["stage"], "installed")
        fetch.assert_not_called()
        install.assert_not_called()
        self.assertEqual(json.loads((self.state / "native.json").read_text()), pending)
        self.assertFalse(list(self.state.glob("superseded-*.json")))

    def test_stale_candidate_with_unsettled_native_writer_is_not_retired(self):
        pending = dict(self.prepared_candidate(),
                       process=dict(execution="unknown", returncode=None, cleanup_failed=False),
                       preparation=dict(execution="completed", returncode=0, cleanup_failed=False),
                       native_output=dict(pluginId=update.PLUGIN, version=self.version))
        (self.state / "native.json").write_text(json.dumps(pending))
        self.installed.return_value = dict(self.before, version=self.next_patch_version())
        with patch("mindie_knowledge.releases_io.fetch_releases") as fetch, \
                patch.object(update, "_native_install") as install:
            value = update.check(self.config)
        self.assertEqual(value["status"], "unknown")
        self.assertEqual(value["stage"], "prepared")
        fetch.assert_not_called()
        install.assert_not_called()
        self.assertEqual(json.loads((self.state / "native.json").read_text()), pending)
        self.assertFalse(list(self.state.glob("superseded-*.json")))

    def test_first_install_does_not_synthesize_active_without_canonical_readback(self):
        pending = self.prepared_candidate()
        with patch.object(update, "run", return_value=ProcessResult(
                "completed", "", 0)) as run:
            with self.assertRaises(ValueError):
                update._prepare_and_activate(
                    self.config, pending, self.env, self.state, first_install=True)
        run.assert_called_once()
        self.assertEqual(
            json.loads((self.state / "native.json").read_text())["activation"]["status"],
            "unknown")

    def test_first_install_readback_reports_active_only_for_exact_binding(self):
        pending = self.prepared_candidate()
        validation = dict(version="fixture", status="validated")
        release = self.root / "first-release"
        release.mkdir()
        (release / "runtime.json").write_text(json.dumps(
            dict(python=sys.executable, validation=validation)))
        engine = self.root / "first-engine.json"
        engine.write_text(json.dumps(dict(
            transcript_adapter=str(Path(pending["runtime"]["scripts"]) / "codex_transcript.py"))))
        self.config.write_text(json.dumps(dict(
            python=sys.executable, runtime_scripts=pending["runtime"]["scripts"],
            engine_config=str(engine), product_validation=validation)))
        pending["runtime"] = dict(python=sys.executable,
                                  scripts=pending["runtime"]["scripts"],
                                  release=str(release))
        with patch.object(update, "run", return_value=ProcessResult(
                "completed", "", 0)):
            value = update._prepare_and_activate(
                self.config, pending, self.env, self.state, first_install=True)
        self.assertEqual(value, {"status": "active"})
        self.assertEqual(
            json.loads((self.state / "native.json").read_text())["activation"]["status"],
            "active")

    def test_first_install_readback_rejects_distinct_venv_paths_with_same_base(self):
        pending = self.prepared_candidate()
        first = self.root / "venv-a/bin/python"
        second = self.root / "venv-b/bin/python"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        try:
            os.symlink(sys.executable, first)
            os.symlink(sys.executable, second)
        except TimeoutError:
            raise
        except OSError as exc:
            self.skipTest(f"symlink fixture unavailable: {type(exc).__name__}")
        self.assertEqual(first.resolve(), second.resolve())
        validation = dict(version="fixture", status="validated")
        release = self.root / "first-release"
        release.mkdir()
        (release / "runtime.json").write_text(json.dumps(
            dict(python=str(first), validation=validation)))
        engine = self.root / "first-engine.json"
        engine.write_text(json.dumps(dict(
            transcript_adapter=str(Path(pending["runtime"]["scripts"]) / "codex_transcript.py"))))
        self.config.write_text(json.dumps(dict(
            python=str(second), runtime_scripts=pending["runtime"]["scripts"],
            engine_config=str(engine), product_validation=validation)))
        pending["runtime"] = dict(python=str(first), scripts=pending["runtime"]["scripts"],
                                  release=str(release))
        self.assertIsNone(update._first_install_readback(self.config, pending["runtime"]))

    def test_native_settled_re_raises_timeout(self):
        pending = self.stale_candidate()
        with patch.object(update, "native_version", side_effect=TimeoutError("busy")):
            with self.assertRaises(TimeoutError):
                update._native_settled(pending)

    def test_prepared_retries_activation_without_network_or_install(self):
        (self.state / "native.json").write_text(json.dumps(dict(
            metadata=self.metadata, stage="prepared", source="/prepared",
            runtime={"fixture": True})))
        self.installed.return_value = dict(self.before, version=self.version)
        with patch("mindie_knowledge.releases_io.fetch_releases",
                   side_effect=AssertionError("network")) as fetch, \
                patch.object(update, "_native_install") as install, \
                patch.object(update, "_prepare_and_activate",
                             return_value=dict(status="deferred", reason="service_busy")) as activate:
            value = update.check(self.config)
        self.assertEqual(value["status"], "deferred")
        activate.assert_called_once()
        install.assert_not_called()
        fetch.assert_not_called()

    def test_prepared_candidate_is_rechecked_against_current_harness(self):
        (self.state / "native.json").write_text(json.dumps(dict(
            metadata=self.metadata, stage="prepared", source="/prepared")))
        with patch.object(update, "harness_version", return_value="0.154.0"), \
                patch.object(update, "_prepare_and_activate") as activate:
            value = update.check(self.config)
        self.assertEqual(value["status"], "failed")
        activate.assert_not_called()

    def test_harness_change_reselects_after_settled_unstarted_candidate(self):
        self.installed.return_value = dict(self.before, version=self.version)
        pending = dict(metadata=self.metadata, stage="prepared", source="/prepared",
                       process=dict(execution="completed", returncode=0, cleanup_failed=False),
                       activation=dict(status="not_started"))
        (self.state / "native.json").write_text(json.dumps(pending))
        new = copy.deepcopy(self.metadata)
        major, minor, patch_version = self.version.split(".")
        new["version"] = f"{major}.{minor}.{int(patch_version) + 1}"
        new["source"]["sha"] = "b" * 40
        supported = [dict(minimum="0.154.0", maximum="0.154.0",
                          maximum_inclusive=True, include_prerelease=False)]
        new["compatibility"]["supported"] = supported
        for value in new["compatibility"]["interface_registry"].values():
            value["supported"] = supported
        release = dict(self.release, tag_name=f'v{new["version"]}', metadata=new, id=2)
        with patch.object(update, "harness_version", return_value="0.154.0"), \
                patch("mindie_knowledge.releases_io.fetch_releases", return_value=[release]) as fetch, \
                patch("mindie_knowledge.releases_io.verify_release_source", return_value=new), \
                patch.object(update, "_native_install", return_value=dict(metadata=new)) as install, \
                patch.object(update, "_prepare_and_activate",
                             return_value=dict(status="deferred", execution="not_started")):
            result = update.check(self.config)
        self.assertEqual(result["status"], "deferred")
        fetch.assert_called_once()
        self.assertEqual(install.call_args.args[3]["version"], new["version"])
        archived = list(self.state.glob("superseded-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text()), pending)

    def test_incompatible_unknown_activation_cannot_be_discarded(self):
        pending = dict(metadata=self.metadata, stage="prepared", source="/prepared",
                       process=dict(execution="completed", returncode=0, cleanup_failed=False),
                       activation=dict(status="unknown"))
        (self.state / "native.json").write_text(json.dumps(pending))
        with patch.object(update, "harness_version", return_value="0.154.0"), \
                patch("mindie_knowledge.releases_io.fetch_releases") as fetch, \
                patch.object(update, "_native_install") as install:
            result = update.check(self.config)
        self.assertEqual(result["status"], "failed")
        fetch.assert_not_called()
        install.assert_not_called()
        self.assertEqual(json.loads((self.state / "native.json").read_text()), pending)

    def test_incompatible_deferred_activation_with_unsettled_writer_cannot_be_discarded(self):
        pending = dict(stage="prepared",
                       process=dict(execution="completed", returncode=0, cleanup_failed=False),
                       activation=dict(status="deferred", execution="not_started",
                                       process=dict(execution="completed", returncode=0,
                                                    cleanup_failed=True)))
        self.assertFalse(update._can_supersede(pending))

    def test_only_successful_complete_check_advances_success_clock(self):
        from mindie_knowledge.releases_io import ReleaseFetchError
        previous = dict(status="failed", checked_at=0, last_successful_check=100)
        (self.state / "status.json").write_text(json.dumps(previous))
        with patch("mindie_knowledge.releases_io.fetch_releases",
                   side_effect=ReleaseFetchError("release_transport_unavailable")):
            value = update.check(self.config)
        self.assertEqual(value["last_successful_check"], 100)
        self.assertEqual(value["code"], "release_transport_unavailable")
        (self.state / "status.json").write_text(json.dumps(previous))
        with patch("mindie_knowledge.releases_io.fetch_releases", return_value=[]):
            value = update.check(self.config)
        self.assertGreater(value["last_successful_check"], 100)
        self.assertEqual(value["status"], "no_compatible_release")
        with patch("mindie_knowledge.releases_io.fetch_releases") as fetch:
            self.assertEqual(update.check(self.config)["status"], "throttled")
        fetch.assert_not_called()

    def test_source_proof_failure_does_not_advance_success_clock(self):
        (self.state / "status.json").write_text(json.dumps(dict(
            checked_at=0, last_successful_check=100, status="failed")))
        with patch("mindie_knowledge.releases_io.fetch_releases", return_value=[self.release]), \
                patch("mindie_knowledge.releases_io.verify_release_source",
                      side_effect=ValueError("private source detail")), \
                patch.object(update, "_native_install") as install:
            value = update.check(self.config)
        self.assertEqual(value["last_successful_check"], 100)
        self.assertNotIn("private source detail", json.dumps(value))
        install.assert_not_called()

    def test_disabled_native_plugin_is_never_enabled_by_install(self):
        self.installed.return_value = dict(self.before, enabled=False)
        with patch("mindie_knowledge.releases_io.fetch_releases") as fetch, \
                patch.object(update, "_native_install") as install:
            self.assertEqual(update.check(self.config)["status"], "disabled")
        fetch.assert_not_called()
        install.assert_not_called()

    def test_registry_resolves_real_code_and_contract_tests(self):
        release_metadata.check_registry(SCRIPTS.parent, SCRIPTS.parents[2] / "tests")

    def test_first_install_requires_actual_supported_harness(self):
        with patch("native_context.capture", return_value=dict(MINDIE_CODEX_BIN="/native")), \
                patch.object(update, "harness_version", return_value="0.154.0"):
            with self.assertRaisesRegex(ValueError, "does not support"):
                release_metadata.require_harness(SCRIPTS.parent)
        for version in ("0.153.4", "0.162.1", "0.164.0", "0.166.0", "0.166.0-alpha.5.1"):
            with self.subTest(version=version), \
                    patch("native_context.capture", return_value=dict(MINDIE_CODEX_BIN="/native")), \
                    patch.object(update, "harness_version", return_value=version):
                self.assertEqual(release_metadata.require_harness(SCRIPTS.parent), version)
        for version in ("0.162.2", "0.164.1", "0.165.0", "0.164.0-alpha.12",
                        "0.166.1", "0.166.0-alpha.6"):
            with self.subTest(version=version), \
                    patch("native_context.capture", return_value=dict(MINDIE_CODEX_BIN="/native")), \
                    patch.object(update, "harness_version", return_value=version):
                with self.assertRaisesRegex(ValueError, "does not support"):
                    release_metadata.require_harness(SCRIPTS.parent)

    def test_trigger_refresh_backoff_and_process_fast_path(self):
        path = self.state / "status.json"
        path.write_text(json.dumps(dict(status="up_to_date", last_successful_check=100)))
        self.assertFalse(update_trigger.due(self.config, 100 + 299))
        self.assertTrue(update_trigger.due(self.config, 100 + 300))
        path.write_text(json.dumps(dict(status="failed", checked_at=100,
                                        last_successful_check=1)))
        self.assertFalse(update_trigger.due(self.config, 159))
        self.assertTrue(update_trigger.due(self.config, 160))
        maintenance = update_trigger.Maintenance(self.config)
        self.addCleanup(maintenance.close)
        maintenance.next_attempt = time.time() + 100
        with patch.object(update_trigger, "next_check", side_effect=AssertionError("disk read")):
            maintenance.trigger()
        self.assertTrue(maintenance.thread.is_alive())

    def test_idle_facade_checks_twice_without_another_rpc(self):
        self.config.write_text(json.dumps(dict(python=sys.executable, runtime_scripts=str(SCRIPTS))))
        maintenance = update_trigger.Maintenance(self.config)
        clock, waits = [100.0], []

        def wait(delay):
            waits.append(delay)
            clock[0] += delay
            return len(waits) > 2

        def helper(*args, **kwargs):
            (self.state / "status.json").write_text(json.dumps(dict(
                status="up_to_date", last_successful_check=clock[0])))
            return ProcessResult("completed", '{"status":"up_to_date"}', 0)

        with patch.object(maintenance.cancel, "wait", side_effect=wait), \
                patch.object(update_trigger.time, "time", side_effect=lambda: clock[0]), \
                patch.object(update_trigger, "run", side_effect=helper) as run, \
                patch.object(update_trigger, "runtime_env", return_value={}):
            maintenance._loop()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(waits, [0, 300, 300])
        self.assertEqual(maintenance.next_attempt, 700)

    def test_trigger_close_cancels_and_joins_owned_runner(self):
        maintenance = update_trigger.Maintenance(self.config)
        self.addCleanup(maintenance.close)
        started = threading.Event()
        def wait_for_cancel():
            started.set()
            maintenance.cancel.wait(5)
        maintenance.thread = threading.Thread(target=wait_for_cancel)
        maintenance.thread.start()
        self.assertTrue(started.wait(1))
        maintenance.close()
        self.assertFalse(maintenance.thread.is_alive())

    def test_missing_status_and_check_busy_still_get_process_backoff(self):
        self.config.write_text(json.dumps(dict(python=sys.executable, runtime_scripts=str(SCRIPTS))))
        for outcome in (RuntimeError("before status"), ProcessResult(
                "completed", '{"status":"deferred","reason":"check_busy"}', 0)):
            with self.subTest(outcome=type(outcome).__name__):
                maintenance = update_trigger.Maintenance(self.config)
                self.addCleanup(maintenance.close)
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(update_trigger, "run", **kwargs) as helper, \
                        patch.object(update_trigger, "runtime_env", return_value={}), \
                        patch("diagnostic_support.failure", return_value={}):
                    maintenance._run()
                    maintenance.trigger()
                    maintenance.trigger()
                helper.assert_called_once()
                self.assertGreater(maintenance.next_attempt, time.time() + 58)
                self.assertTrue(maintenance.thread.is_alive())

    def test_nonfinite_timestamp_is_not_a_successful_future_check(self):
        for value in (float("nan"), float("inf"), True):
            (self.state / "status.json").write_text(json.dumps(dict(
                status="up_to_date", last_successful_check=value)))
            with self.assertRaises(ValueError):
                update_trigger.next_check(self.config)

    def test_ten_thousand_hot_triggers_do_no_io_or_process_work(self):
        (self.state / "status.json").write_text(json.dumps(dict(
            status="up_to_date", last_successful_check=time.time())))
        maintenance = update_trigger.Maintenance(self.config)
        self.addCleanup(maintenance.close)
        with patch.object(update_trigger, "read_optional", wraps=update_trigger.read_optional) as read:
            maintenance.trigger()
            read.assert_called_once()
        with patch.object(update_trigger, "next_check",
                          side_effect=AssertionError("repeated receipt read")), \
                patch.object(update_trigger, "run",
                             side_effect=AssertionError("subprocess")), \
                patch("mindie_knowledge.releases_io.fetch_releases",
                      side_effect=AssertionError("network")):
            started = time.perf_counter()
            for _ in range(10000):
                maintenance.trigger()
            elapsed = time.perf_counter() - started
        self.assertTrue(maintenance.thread.is_alive())
        print(json.dumps(dict(test="10000_hot_triggers", wall_seconds=elapsed,
                              receipt_reads=0, subprocesses=0, network_requests=0)))

    def test_twenty_concurrent_due_triggers_start_one_worker(self):
        self.config.write_text(json.dumps(dict(python=sys.executable, runtime_scripts=str(SCRIPTS))))
        maintenance = update_trigger.Maintenance(self.config)
        self.addCleanup(maintenance.close)
        barrier = threading.Barrier(20)
        started, release = threading.Event(), threading.Event()

        def helper(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError("fixture release missing")
            return ProcessResult("completed", '{"status":"deferred","reason":"check_busy"}', 0)

        def trigger():
            barrier.wait(5)
            maintenance.trigger()

        threads = [threading.Thread(target=trigger) for _ in range(20)]
        with patch.object(update_trigger, "run", side_effect=helper) as run, \
                patch.object(update_trigger, "runtime_env", return_value={}):
            try:
                for thread in threads:
                    thread.start()
                self.assertTrue(started.wait(5))
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(run.call_count, 1)
            finally:
                release.set()
                maintenance.close()
        self.assertGreater(maintenance.next_attempt, time.time() + 58)

    def test_prepared_retry_uses_short_independent_deadline(self):
        (self.state / "status.json").write_text(json.dumps(dict(
            status="deferred", checked_at=100, last_successful_check=100)))
        self.assertEqual(update_trigger.next_check(self.config), 105)
        self.assertFalse(update_trigger.due(self.config, 104))
        self.assertTrue(update_trigger.due(self.config, 105))

    def test_disabled_choice_is_throttled_without_advancing_success(self):
        (self.state / "status.json").write_text(json.dumps(dict(
            status="disabled", checked_at=100, last_successful_check=1)))
        self.assertEqual(update_trigger.next_check(self.config), 160)


if __name__ == "__main__":
    unittest.main()
