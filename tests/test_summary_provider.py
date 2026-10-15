"""Selected routing survives isolated native indexing without carrying user tools."""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import agent_worker as worker
import process_guard
import summary_provider as provider
from k3_material_fixture import request, result, completion


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, CODEX_HOME=str(self.home))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)

    def config(self, *, token="SYNTHETIC_PRIVATE_TOKEN", url="http://127.0.0.1:3123/v1",
               extra="", catalog=True, native_auth=False):
        text = (
            'model="expensive-business-model"\nmodel_reasoning_effort="xhigh"\n'
            'model_provider="selected"\n'
        )
        if catalog:
            path = self.home / "models.json"
            path.write_text(json.dumps({"models": [
                {"slug": "other-business-model"}, {"slug": "vendor/gpt-5.6-luna"}]}),
                encoding="utf-8")
            text += "model_catalog_json=" + json.dumps(str(path)) + "\n"
        text += ('[model_providers.selected]\nname="Private provider"\n'
                 + "base_url=" + json.dumps(url) + "\n"
                 + "experimental_bearer_token=" + json.dumps(token) + "\n"
                 + 'wire_api="responses"\n'
                 + "requires_openai_auth=" + str(native_auth).lower() + "\n"
                 + extra + "\n"
                 + '[model_providers.unselected]\nname="Never copy"\n'
                 + 'experimental_bearer_token="OTHER_PROVIDER_PRIVATE"\n'
                 + '[mcp_servers.private]\ncommand="PRIVATE_MCP"\n'
                 + '[hooks]\nprivate="PRIVATE_HOOK"\n'
                 + '[features]\nshell_tool=true\n')
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def invoke(self, payload):
        observed = {}

        def native(command, prompt, **options):
            observed["command"] = command
            observed["env"] = options["env"]
            child_home = Path(options["env"]["CODEX_HOME"])
            observed["home"] = child_home
            observed["config"] = tomllib.loads((child_home / "config.toml").read_text(encoding="utf-8"))
            observed["files"] = sorted(path.name for path in child_home.iterdir())
            observed["mode"] = stat.S_IMODE((child_home / "config.toml").stat().st_mode)
            completion(options["receipt"])
            options["receipt"]["final_message"] = json.dumps(result(payload))

        with patch.object(worker, "run_codex", native):
            value = worker.run(payload)
        return value, observed

    def test_custom_provider_is_private_and_only_selected_routing_crosses(self):
        self.config(extra='http_headers={Authorization="SYNTHETIC_HEADER"}')
        (self.home / "auth.json").write_text('{"private":"NEVER_COPY_AUTH"}', encoding="utf-8")
        payload = request(worker)
        value, seen = self.invoke(payload)
        self.assertEqual(value["status"], "returned")
        self.assertIn("vendor/gpt-5.6-luna", seen["command"])
        self.assertIn('model_reasoning_effort="low"', seen["command"])
        self.assertNotIn("--ignore-user-config", seen["command"])
        self.assertIn("features.plugins=false", seen["command"])
        self.assertIn("features.skip_host_skill_discovery=true", seen["command"])
        self.assertIn("suppress_unstable_features_warning=true", seen["command"])
        self.assertIn("features.skill_search=false", seen["command"])
        self.assertEqual(set(seen["config"]), {"model_provider", "model_providers"})
        self.assertEqual(set(seen["config"]["model_providers"]), {"selected"})
        self.assertEqual(seen["files"], ["config.toml"])
        if os.name == "posix":
            self.assertEqual(seen["mode"], 0o600)
        self.assertFalse(seen["home"].exists())
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", json.dumps(seen["command"]))
        self.assertNotIn("SYNTHETIC_HEADER", json.dumps(seen["command"]))
        self.assertNotIn("OTHER_PROVIDER_PRIVATE", json.dumps(seen["config"]))
        self.assertNotIn("PRIVATE_MCP", json.dumps(seen["config"]))
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", json.dumps(payload))
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", json.dumps(value))

    def test_auth_rotation_does_not_invalidate_policy_but_route_change_does(self):
        self.config()
        before = worker.identity()
        self.config(token="ROTATED_PRIVATE")
        self.assertEqual(before, worker.identity())
        self.config(token="ROTATED_PRIVATE", url="http://127.0.0.1:4123/v1")
        self.assertNotEqual(before, worker.identity())

    def test_route_query_is_preserved_and_only_known_credentials_ignore_rotation(self):
        self.config(url="http://127.0.0.1:3123/v1?api-version=2026&api_key=first",
                    extra='query_params={tenant="one",access_token="first"}')
        before = worker.identity()
        self.config(url="http://127.0.0.1:3123/v1?api-version=2026&api_key=second",
                    extra='query_params={tenant="one",access_token="second"}')
        self.assertEqual(before, worker.identity())
        payload = request(worker)
        value, seen = self.invoke(payload)
        projected = seen["config"]["model_providers"]["selected"]
        self.assertEqual(projected["query_params"], {"tenant": "one", "access_token": "second"})
        self.assertTrue(projected["base_url"].endswith("api_key=second"))
        self.assertNotIn("second", json.dumps(worker.identity()))
        self.config(url="http://127.0.0.1:3123/v1?api-version=2027&api_key=second",
                    extra='query_params={tenant="one",access_token="second"}')
        self.assertNotEqual(before, worker.identity())
        self.config(url="http://127.0.0.1:3123/v1?api-version=2026&api_key=second",
                    extra='query_params={tenant="two",access_token="second"}')
        self.assertNotEqual(before, worker.identity())
        self.assertEqual(value["status"], "returned")

    def test_missing_auth_environment_or_unsupported_provider_field_fails_before_spawn(self):
        self.config()
        payload = request(worker)
        with patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                    if key != "MINDIE_TEST_MISSING_AUTH"}, clear=True):
            for extra in ('env_key="MINDIE_TEST_MISSING_AUTH"',
                          'env_http_headers={Authorization="MINDIE_TEST_MISSING_AUTH"}',
                          'credential_helper="DO_NOT_RUN"',
                          'auth_command=["DO_NOT_RUN"]'):
                with self.subTest(extra=extra):
                    self.config(extra=extra)
                    with patch.object(worker, "run_codex") as native:
                        value = worker.run(payload)
                    self.assertEqual(value["error_reason"], "configuration_invalid")
                    self.assertEqual(value["model_calls"], 0)
                    native.assert_not_called()
        self.config(extra='env_key="MINDIE_TEST_AUTH"')
        with patch.dict(os.environ, MINDIE_TEST_AUTH="first"):
            before = worker.identity()
        with patch.dict(os.environ, MINDIE_TEST_AUTH="second"):
            self.assertEqual(before, worker.identity())

    def test_noncredential_headers_change_policy_but_auth_headers_do_not(self):
        self.config(extra='http_headers={Authorization="first",x-routing-region="west"}')
        before = worker.identity()
        self.config(extra='http_headers={Authorization="second",x-routing-region="west"}')
        self.assertEqual(before, worker.identity())
        self.config(extra='http_headers={Authorization="second",x-routing-region="east"}')
        self.assertNotEqual(before, worker.identity())
        self.config(extra='env_http_headers={Authorization="MINDIE_TEST_AUTH",x-routing-region="MINDIE_TEST_ROUTE"}')
        with patch.dict(os.environ, MINDIE_TEST_AUTH="first", MINDIE_TEST_ROUTE="west"):
            before = worker.identity()
        with patch.dict(os.environ, MINDIE_TEST_AUTH="second", MINDIE_TEST_ROUTE="west"):
            self.assertEqual(before, worker.identity())
        with patch.dict(os.environ, MINDIE_TEST_AUTH="second", MINDIE_TEST_ROUTE="east"):
            self.assertNotEqual(before, worker.identity())

    def test_selected_native_profile_is_not_silently_treated_as_top_level_provider(self):
        self.config()
        payload = request(worker)
        for text in ('profile="other"\n',
                     'profile="other"\nmodel_provider="selected"\n'
                     '[model_providers.selected]\nname="Fixture"\nbase_url="http://127.0.0.1:1"\n'):
            with self.subTest(text=text):
                (self.home / "config.toml").write_text(text, encoding="utf-8")
                with patch.object(worker, "run_codex") as native:
                    value = worker.run(payload)
                self.assertEqual(value["error_reason"], "configuration_invalid")
                self.assertEqual(value["model_calls"], 0)
                native.assert_not_called()

    def test_missing_catalog_keeps_exact_fixed_model_without_guessing_prefix(self):
        self.config(catalog=False)
        self.assertEqual(provider.selected(worker.SUMMARY_MODEL)["model"], "gpt-5.6-luna")

    def test_standard_default_home_selects_configured_provider_without_explicit_env(self):
        home = self.home / ".codex"
        home.mkdir()
        self.config(catalog=False)
        shutil.move(self.home / "config.toml", home / "config.toml")
        with patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                    if key != "CODEX_HOME"}, clear=True), \
                patch.object(provider.Path, "home", return_value=self.home):
            self.assertEqual(provider.selected(worker.SUMMARY_MODEL)["name"], "selected")

    def test_supplied_catalog_rejects_missing_ambiguous_or_corrupt_models(self):
        self.config()
        path = self.home / "models.json"
        for data in ({"models": [{"slug": "different"}]},
                     {"models": [{"slug": "vendor/gpt-5.6-luna"}, {"slug": "other/gpt-5.6-luna"}]},
                     {"models": [{"id": "vendor/gpt-5.6-luna"}]},
                     {"models": "invalid"}):
            with self.subTest(data=data):
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(provider.ProviderConfigurationError):
                    provider.selected(worker.SUMMARY_MODEL)

    def test_builtin_keeps_existing_auth_home_without_reading_or_copying_credentials(self):
        (self.home / "config.toml").write_text('model="business"\n', encoding="utf-8")
        route = provider.selected(worker.SUMMARY_MODEL)
        self.assertEqual(route, {"model": "gpt-5.6-luna", "provider": None})
        with patch.object(provider, "_read", side_effect=AssertionError("no auth reads")):
            self.assertIsNone(provider.environment(route, self.home / "unused"))

    def test_custom_native_auth_requirement_fails_before_spawn(self):
        self.config()
        payload = request(worker)
        self.config(native_auth=True)
        with patch.object(worker, "run_codex") as native:
            value = worker.run(payload)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["error"], "configuration")
        self.assertEqual(value["error_reason"], "configuration_invalid")
        self.assertEqual(value["model_calls"], 0)
        native.assert_not_called()

    def test_bad_selected_provider_never_falls_back_to_builtin(self):
        self.config()
        payload = request(worker)
        for text in ('model_provider="missing"\n', "broken configuration [",
                     'model_provider="selected"\n[model_providers.selected]\nbase_url=42\n'):
            with self.subTest(text=text):
                (self.home / "config.toml").write_text(text, encoding="utf-8")
                with patch.object(worker, "run_codex") as native:
                    value = worker.run(payload)
                self.assertEqual(value["error"], "configuration")
                self.assertEqual(value["model_calls"], 0)
                native.assert_not_called()

    def test_changed_route_does_not_start_previously_prepared_request(self):
        self.config()
        payload = request(worker)
        self.config(url="http://127.0.0.1:4123/v1")
        with patch.object(worker, "run_codex") as native:
            value = worker.run(payload)
        self.assertEqual(value["error_reason"], "worker_policy_changed")
        native.assert_not_called()

    def test_cleanup_failure_preserves_completed_result_and_usage(self):
        self.config()
        payload = request(worker)
        with patch.object(worker.shutil, "rmtree", side_effect=PermissionError("PRIVATE_PATH")), \
                patch("diagnostic_support.failure") as failure:
            value, seen = self.invoke(payload)
        self.assertEqual(value["status"], "returned")
        self.assertTrue(value["cleanup_failed"])
        self.assertEqual(value["result"], result(payload))
        self.assertEqual(value["usage"]["input_tokens"], 120)
        self.assertNotIn("PRIVATE_PATH", json.dumps(value))
        failure.assert_called_once()
        self.assertFalse(failure.call_args.kwargs["reportable"])
        shutil.rmtree(seen["home"].parent)

    def test_cleanup_failure_does_not_replace_primary_native_failure(self):
        self.config()
        payload = request(worker)
        retained = []

        def native(command, prompt, **options):
            retained.append(Path(options["env"]["CODEX_HOME"]).parent)
            self.addCleanup(lambda path=retained[-1]: shutil.rmtree(path))
            options["receipt"].update(native_started=True, turn_started=True, turn_failed=True,
                                      usage={"input_tokens": 4, "output_tokens": 0})
            raise worker.NativeFailure("synthetic native failure", reason="native_turn_failed")

        with patch.object(worker, "run_codex", native), \
                patch.object(worker.shutil, "rmtree", side_effect=PermissionError("PRIVATE_PATH")), \
                patch("diagnostic_support.failure"):
            value = worker.run(payload)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["error_reason"], "native_turn_failed")
        self.assertEqual(value["usage"]["input_tokens"], 4)
        self.assertTrue(value["cleanup_failed"])
        self.assertNotIn("PRIVATE_PATH", json.dumps(value))

    def test_nonregular_or_oversized_config_is_rejected_before_read(self):
        path = self.home / "config.toml"
        path.mkdir()
        with self.assertRaises(provider.ProviderConfigurationError):
            provider.selected(worker.SUMMARY_MODEL)
        path.rmdir()
        path.write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaises(provider.ProviderConfigurationError):
            provider.selected(worker.SUMMARY_MODEL)

    def test_owned_native_process_receives_only_the_requested_child_environment(self):
        code = (
            "import json,os\n"
            "print(json.dumps({'type':'turn.started'}),flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message',"
            "'text':os.environ['MINDIE_SUMMARY_ENV_CANARY']}}),flush=True)\n"
            "print(json.dumps({'type':'turn.completed','usage':"
            "{'input_tokens':1,'cached_input_tokens':0,'output_tokens':1}}),flush=True)\n"
        )
        receipt = {}
        process_guard.run_codex([sys.executable, "-c", code], "fixture", receipt=receipt,
                                env=dict(os.environ, MINDIE_SUMMARY_ENV_CANARY="selected-child"))
        self.assertTrue(receipt["turn_completed"])
        self.assertEqual(receipt["final_message"], "selected-child")
        self.assertNotIn("MINDIE_SUMMARY_ENV_CANARY", os.environ)


if __name__ == "__main__":
    unittest.main()
