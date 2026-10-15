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


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.json"
        self.env = dict(CODEX_HOME=str(self.root / "native"),
                        MINDIE_CODEX_BIN="/fixture/codex")
        self.config.write_text(json.dumps(dict(native_context=self.env)))
        self.metadata = release_metadata.metadata(SCRIPTS.parent, "a" * 40)
        self.release = dict(id=1, tag_name="v0.0.1", draft=False, prerelease=False,
                            metadata=self.metadata)
        self.state = update_trigger.directory(self.config)
        self.state.mkdir()
        self.before = dict(version="0.0.0", enabled=True,
                           marketplaceSource=dict(sourceType="local",
                                                  source=str(self.state / "catalog")))
        self.patch("harness_version", "0.153.4")
        self.patch("runtime_env", self.env)
        self.installed = self.patch("installed", self.before)

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
        source = Path(self.env["CODEX_HOME"]) / "plugins/cache/mindie-agent/mindie-agent/0.0.1"
        (source / ".codex-plugin").mkdir(parents=True)
        (source / ".codex-plugin/plugin.json").write_text(json.dumps(dict(version="0.0.1")))
        (source / "pyproject.toml").write_text('[project]\nversion="0.0.1"\n')
        (source / "harness-compatibility.json").write_text(json.dumps(self.metadata["compatibility"]))
        self.installed.return_value = dict(self.before, version="0.0.1")
        output = dict(pluginId=update.PLUGIN, version="0.0.1", installedPath=str(source))
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
        self.installed.return_value = dict(self.before, version="0.0.1")
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
        self.installed.return_value = dict(self.before, version="0.0.1")
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
        (source / ".codex-plugin/plugin.json").write_text(json.dumps(dict(version="0.0.1")))
        (source / "pyproject.toml").write_text('[project]\nversion="0.0.1"\n')
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

    def prepared_candidate(self):
        source = self.root / "source"
        (source / ".codex-plugin").mkdir(parents=True)
        (source / ".codex-plugin/plugin.json").write_text(json.dumps(dict(version="0.0.1")))
        (source / "pyproject.toml").write_text('[project]\nversion="0.0.1"\n')
        (source / "harness-compatibility.json").write_text(json.dumps(self.metadata["compatibility"]))
        return dict(metadata=self.metadata, stage="prepared", source=str(source),
                    runtime=dict(python="/candidate/python", scripts="/candidate/scripts",
                                 release="/candidate/release"),
                    hook_trust=dict(status="complete"),
                    activation=dict(status="not_started"))

    def test_activation_unknown_writer_never_starts_another_control(self):
        for activation in (
            dict(status="unknown", process=None),
            dict(status="unknown", process=dict(execution="unknown", returncode=None,
                                               cleanup_failed=True)),
        ):
            with self.subTest(activation=activation), patch.object(update, "run") as run:
                pending = dict(source="/not-read", activation=activation)
                value = update._prepare_and_activate(self.config, pending, self.env, self.state)
                self.assertEqual(value["status"], "unknown")
                self.assertEqual(value["activation"], activation)
                run.assert_not_called()

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
        with patch.object(update, "run") as run:
            value = update._prepare_and_activate(self.config, saved, self.env, self.state)
        self.assertEqual(value["status"], "unknown")
        run.assert_not_called()

    def test_settled_activation_reconciliation_preserves_result_if_recording_fails(self):
        pending = self.prepared_candidate()
        pending["activation"] = dict(status="unknown", process=dict(
            execution="unknown", returncode=-9, cleanup_failed=False))
        with patch.object(update, "run", return_value=ProcessResult(
                "completed", '{"status":"active"}', 0)) as run, \
                patch.object(update, "_atomic", side_effect=[None, OSError("private path")]):
            with self.assertRaises(OSError) as caught:
                update._prepare_and_activate(self.config, pending, self.env, self.state)
        run.assert_called_once()
        self.assertEqual(caught.exception.native_effect["activation"], dict(
            status="active", process=dict(execution="completed", returncode=0, cleanup_failed=False)))
        self.assertEqual(caught.exception.receipt_error_type, "OSError")

    def test_prepared_retries_activation_without_network_or_install(self):
        (self.state / "native.json").write_text(json.dumps(dict(
            metadata=self.metadata, stage="prepared", source="/prepared",
            runtime={"fixture": True})))
        self.installed.return_value = dict(self.before, version="0.0.1")
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
        self.installed.return_value = dict(self.before, version="0.0.1")
        pending = dict(metadata=self.metadata, stage="prepared", source="/prepared",
                       process=dict(execution="completed", returncode=0, cleanup_failed=False),
                       activation=dict(status="not_started"))
        (self.state / "native.json").write_text(json.dumps(pending))
        new = copy.deepcopy(self.metadata)
        new["version"] = "0.0.2"
        new["source"]["sha"] = "b" * 40
        supported = [dict(minimum="0.154.0", maximum="0.154.0",
                          maximum_inclusive=True, include_prerelease=False)]
        new["compatibility"]["supported"] = supported
        for value in new["compatibility"]["interface_registry"].values():
            value["supported"] = supported
        release = dict(self.release, tag_name="v0.0.2", metadata=new, id=2)
        with patch.object(update, "harness_version", return_value="0.154.0"), \
                patch("mindie_knowledge.releases_io.fetch_releases", return_value=[release]) as fetch, \
                patch("mindie_knowledge.releases_io.verify_release_source", return_value=new), \
                patch.object(update, "_native_install", return_value=dict(metadata=new)) as install, \
                patch.object(update, "_prepare_and_activate",
                             return_value=dict(status="deferred", execution="not_started")):
            result = update.check(self.config)
        self.assertEqual(result["status"], "deferred")
        fetch.assert_called_once()
        self.assertEqual(install.call_args.args[3]["version"], "0.0.2")
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
        with patch("native_context.capture", return_value=dict(MINDIE_CODEX_BIN="/native")), \
                patch.object(update, "harness_version", return_value="0.153.4"):
            self.assertEqual(release_metadata.require_harness(SCRIPTS.parent), "0.153.4")

    def test_trigger_24h_backoff_and_process_fast_path(self):
        path = self.state / "status.json"
        path.write_text(json.dumps(dict(status="up_to_date", last_successful_check=100)))
        self.assertFalse(update_trigger.due(self.config, 100 + 86399))
        self.assertTrue(update_trigger.due(self.config, 100 + 86400))
        path.write_text(json.dumps(dict(status="failed", checked_at=100,
                                        last_successful_check=1)))
        self.assertFalse(update_trigger.due(self.config, 159))
        self.assertTrue(update_trigger.due(self.config, 160))
        maintenance = update_trigger.Maintenance(self.config)
        maintenance.next_attempt = time.time() + 100
        with patch.object(update_trigger, "next_check", side_effect=AssertionError("disk read")):
            maintenance.trigger()
        self.assertIsNone(maintenance.thread)

    def test_trigger_close_cancels_and_joins_owned_runner(self):
        maintenance = update_trigger.Maintenance(self.config)
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
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(update_trigger, "run", **kwargs) as helper, \
                        patch.object(update_trigger, "runtime_env", return_value={}), \
                        patch("diagnostic_support.failure", return_value={}):
                    maintenance._run()
                    maintenance.trigger()
                    maintenance.trigger()
                helper.assert_called_once()
                self.assertGreater(maintenance.next_attempt, time.time() + 58)
                self.assertIsNone(maintenance.thread)

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
        self.assertIsNone(maintenance.thread)
        print(json.dumps(dict(test="10000_hot_triggers", wall_seconds=elapsed,
                              receipt_reads=0, subprocesses=0, network_requests=0)))

    def test_twenty_concurrent_due_triggers_start_one_worker(self):
        self.config.write_text(json.dumps(dict(python=sys.executable, runtime_scripts=str(SCRIPTS))))
        maintenance = update_trigger.Maintenance(self.config)
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
