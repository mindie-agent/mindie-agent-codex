"""Idle switching preserves exact service facts, without a model or native install."""
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import release_activation
from session_gate import runtime_binding

OFFICIAL_OPERATORS = dict(
    domain="operators", repository="mindie-agent/knowledge-operators",
    ref="main", interval_seconds=300)
OFFICIAL_VLLM = dict(
    domain="vllm-ascend", repository="mindie-agent/knowledge-vllm-ascend",
    ref="main", interval_seconds=300)
VALIDATION = dict(schema="mindie-candidate-check/1", publication=dict(
    repository="mindie-agent/knowledge-vllm-ascend", ref="main",
    domain="vllm-ascend", verified_commit="1" * 40))


def _engine(root, domain="vllm-ascend", *, feeds=None, community=None):
    return dict(
        root=str(root), domain=domain, admission_path=str(root.parent / "admission.sqlite3"),
        transcript_adapter=str(SCRIPTS / "codex_transcript.py"),
        redactor_executable=sys.executable,
        summary_command=[sys.executable, str(SCRIPTS / "agent_worker.py")],
        community_config=str(community or root.parent / "mindie-community.json"),
        service_launch_context={"paths": {"MINDIE_AGENT_CONFIG": str(root.parent / "codex.json")},
                                "environment": []},
        feeds=list(feeds or []),
    )


class OfficialFeedUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.scripts = SCRIPTS

    def profile(self, *, feeds, domain_configs=None):
        config = self.base / "codex.json"
        engine_path = self.base / "codex.engine.json"
        engine = _engine(self.base / "data", feeds=feeds)
        engine_path.write_text(json.dumps(engine), encoding="utf-8")
        adapter = dict(
            python=sys.executable, engine_config=str(engine_path),
            community_config=str(self.base / "mindie-community.json"),
            admission_path=str(self.base / "admission.sqlite3"),
            runtime_scripts=str(self.scripts), product_validation={},
            domain_configs=dict(domain_configs or {}),
        )
        config.write_text(json.dumps(adapter), encoding="utf-8")
        from mindie_knowledge import consent_store
        consent_store.record_choice(self.base / "mindie-consent.json", "disabled")
        return config, self.base / "mindie-consent.json"

    def prepare(self, config, name="release-a", validation=VALIDATION):
        release = self.base / name
        release.mkdir()
        (release / "runtime.json").write_text(json.dumps(dict(
            python=sys.executable, validation=validation)), encoding="utf-8")
        with patch("capture_config.prepare", return_value=dict(
                capture_mode="public-transcript", redactor_executable=sys.executable,
                summary_command=[sys.executable, str(self.scripts / "agent_worker.py")])), \
             patch("native_context.service_launch_context", return_value={
                 "paths": {"MINDIE_AGENT_CONFIG": str(config)}, "environment": []}), \
             patch("summary_provider.required_environment"):
            return release_activation.prepare(config, release)

    def test_update_adds_disabled_operators_route_on_main(self):
        config, consent = self.profile(feeds=[dict(OFFICIAL_VLLM)])
        before = consent.read_bytes()
        prepared = self.prepare(config)
        engine = json.loads(Path(prepared["adapter"]["engine_config"]).read_text())
        self.assertEqual(engine["feeds"], [OFFICIAL_VLLM])
        operators_path = prepared["adapter"]["domain_configs"]["operators"]
        operators = json.loads(Path(operators_path).read_text())
        self.assertEqual(operators["feeds"], [OFFICIAL_OPERATORS])
        settings = json.loads(Path(operators["community_config"]).read_text())
        self.assertEqual(settings["branch"], "main")
        self.assertEqual(settings["repository"], "mindie-agent/knowledge-operators")
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["consent_config"], str(consent))
        self.assertEqual(consent.read_bytes(), before)

    def test_custom_root_ref_and_route_survive_update(self):
        custom_root = dict(repository="owner/custom-vllm", ref="topic",
                           domain="vllm-ascend", interval_seconds=300)
        custom_operators = dict(repository="owner/custom-operators", ref="review",
                                domain="operators", interval_seconds=300)
        operators_engine = _engine(
            self.base / "data", "operators", feeds=[custom_operators],
            community=self.base / "knowledge-domains/operators/community.json")
        operators_engine["domain_catalog"] = "mindie-domain-catalog/1"
        operators_path = self.base / "custom-operators.engine.json"
        operators_path.write_text(json.dumps(operators_engine), encoding="utf-8")
        settings_path = self.base / "knowledge-domains/operators/community.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps(dict(
            enabled=False, repository=custom_operators["repository"],
            branch=custom_operators["ref"], project_roots=[], idle_seconds=300,
            consent_config=str(self.base / "mindie-consent.json"),
            visibility="public", schema="mindie-community-config/1",
            generation="custom-generation", enabled_at=None)), encoding="utf-8")
        config, consent = self.profile(
            feeds=[custom_root], domain_configs={"operators": str(operators_path)})
        settings_before = settings_path.read_bytes()
        before = consent.read_bytes()
        prepared = self.prepare(config)
        engine = json.loads(Path(prepared["adapter"]["engine_config"]).read_text())
        self.assertEqual(engine["feeds"], [custom_root])
        prepared_operators = json.loads(
            Path(prepared["adapter"]["domain_configs"]["operators"]).read_text())
        self.assertEqual(prepared_operators["feeds"], [custom_operators])
        self.assertEqual(settings_path.read_bytes(), settings_before)
        self.assertEqual(consent.read_bytes(), before)

    def test_component_candidate_prepares_new_commands_without_changing_the_live_profile(self):
        config, consent = self.profile(feeds=[dict(OFFICIAL_VLLM)])
        old = json.loads(config.read_text())
        old["component_commands"] = {
            "sim": ["/old/python", "-I", "-m", "mindie_sim"],
            "profiler": ["/custom/profiler", "--format", "site"],
        }
        config.write_text(json.dumps(old))
        before = config.read_bytes(), consent.read_bytes()
        validation = dict(VALIDATION, component_updates={"runtime": {"mindie-sim": {
            "repository": "mindie-agent/sim", "version": "0.7.1", "revision": "a" * 40}}})
        with patch("mindie_knowledge.component_bindings.read_versions",
                   return_value={"mindie-sim": "0.7.1"}):
            prepared = self.prepare(config, validation=validation)
        assert prepared["adapter"]["component_commands"]["sim"] == [
            sys.executable, "-I", "-X", "utf8", "-m", "mindie_sim"]
        assert prepared["adapter"]["component_commands"]["profiler"] == old["component_commands"]["profiler"]
        assert prepared["expected_binding"]["component_commands"] == old["component_commands"]
        assert (config.read_bytes(), consent.read_bytes()) == before
        with patch("mindie_knowledge.component_bindings.read_versions",
                   return_value={"mindie-sim": "0.8.0"}):
            with self.assertRaisesRegex(ValueError, "below the current bound component"):
                self.prepare(config, name="stale-candidate", validation=validation)
        assert (config.read_bytes(), consent.read_bytes()) == before


class ActivationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "codex.json"
        self.old = dict(python="/old/python", runtime_scripts="/old/scripts",
                        engine_config=str(self.root / "old.json"),
                        product_validation={"old": True}, consent="preserved")
        self.new = dict(self.old, python="/new/python", runtime_scripts="/new/scripts",
                        engine_config=str(self.root / "new.json"),
                        product_validation={"new": True})
        self.config.write_text(json.dumps(self.old))
        self.prepared = dict(adapter=self.new, expected_binding=runtime_binding(self.old))
        self.original = self.config.read_bytes()
        for module, name, value in (
            ("mindie_knowledge.loop.cli", "config_at", {}),
            ("mindie_knowledge.loop.lifecycle", "inspect_retirement", None),
            ("mindie_knowledge.loop.lifecycle", "retire_service", {"idle": True}),
            ("mindie_knowledge.loop.cli", "prepare_service", "starting"),
        ):
            mock = patch(module + "." + name, return_value=value).start()
            self.addCleanup(patch.stopall)
            setattr(self, name, mock)

    def run_activation(self, states):
        with patch("mindie_knowledge.loop.handoff._probe", side_effect=states):
            return release_activation.activate(self.config, self.prepared, control_seconds=.5)

    def test_isolated_installed_entry_resolves_its_own_modules(self):
        result = subprocess.run(
            [sys.executable, "-I", "-c",
             "import runpy,sys;runpy.run_path(sys.argv[1],run_name='isolated_entry_test')",
             str(SCRIPTS / "release_activation.py")],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_idle_prepare_ready_then_publish_preserves_consent(self):
        result = self.run_activation(["absent", "ready"])
        self.assertEqual(result["status"], "active")
        self.retire_service.assert_called_once()
        self.prepare_service.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text()), self.new)

    def test_busy_leaves_old_binding_and_never_starts(self):
        self.retire_service.return_value = {"idle": False}
        result = self.run_activation([])
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.prepare_service.assert_not_called()

    def test_component_commands_switch_only_after_idle_and_detect_concurrent_edits(self):
        self.old["component_commands"] = {"sim": ["/old/python", "-m", "mindie_sim"]}
        self.new["component_commands"] = {"sim": ["/new/python", "-m", "mindie_sim"]}
        self.config.write_text(json.dumps(self.old))
        self.prepared["expected_binding"] = runtime_binding(self.old)
        self.retire_service.return_value = {"idle": False}
        assert self.run_activation([])["status"] == "deferred"
        assert json.loads(self.config.read_text()) == self.old
        self.retire_service.return_value = {"idle": True}
        assert self.run_activation(["absent"])["status"] == "active"
        assert json.loads(self.config.read_text()) == self.new
        changed = dict(self.old, component_commands={"sim": ["/user/new-wrapper"]})
        self.config.write_text(json.dumps(changed))
        assert self.run_activation([])["reason"] == "installation_changed"
        assert json.loads(self.config.read_text()) == changed

    def test_busy_secondary_domain_prevents_publication_until_its_owner_retires(self):
        self.old['domain_configs'] = {'operators': str(self.root / 'old-operators.json')}
        self.new['domain_configs'] = {'operators': str(self.root / 'new-operators.json')}
        self.config.write_text(json.dumps(self.old))
        self.original = self.config.read_bytes()
        self.prepared['expected_binding'] = runtime_binding(self.old)
        self.retire_service.side_effect = [{'idle': True}, {'idle': False}]
        result = self.run_activation(['absent', 'ready'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['stage'], 'domain_retirement')
        self.assertEqual(result['domains']['status'], 'deferred')
        self.assertEqual(self.config.read_bytes(), self.original)

        self.inspect_retirement.return_value = {'status': 'retired'}
        self.retire_service.side_effect = [{'idle': True}]
        result = self.run_activation(['ready'])
        self.assertEqual(result['status'], 'active')
        self.assertEqual(json.loads(self.config.read_text()), self.new)
        self.assertEqual(self.retire_service.call_args.args[0], self.old['domain_configs']['operators'])

    def test_retired_cleanup_failure_remains_incomplete_without_start(self):
        self.inspect_retirement.return_value = {"status": "retired"}
        self.retire_service.return_value = {"idle": True, "cleanup_failed": True}
        result = self.run_activation(["absent"])
        self.assertEqual(result["stage"], "old_owner_retirement")
        self.assertTrue(result["retirement"]["cleanup_failed"])
        self.prepare_service.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_retired_absent_publishes_without_starting_knowledge(self):
        self.inspect_retirement.return_value = {"status": "retired"}
        result = self.run_activation(["absent", "absent", "ready"])
        self.assertEqual(result["status"], "active")
        self.retire_service.assert_called_once()
        self.prepare_service.assert_not_called()

    def test_ready_after_write_failure_only_finishes_publication(self):
        self.inspect_retirement.return_value = {"status": "retired"}
        with patch.object(release_activation, "_atomic", side_effect=OSError("private detail")):
            result = self.run_activation(["ready"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stage"], "binding_publish")
        self.assertEqual(result["service"], "ready")
        self.assertNotIn("private detail", json.dumps(result))
        self.assertEqual(self.config.read_bytes(), self.original)
        result = self.run_activation(["ready"])
        self.assertEqual(result["status"], "active")
        self.retire_service.assert_not_called()
        self.prepare_service.assert_not_called()

    def test_unknown_or_starting_candidate_is_not_started_again(self):
        self.inspect_retirement.return_value = {"status": "retired"}
        for state in ("unknown", "starting", "worker-dead", "stopping"):
            with self.subTest(state=state):
                result = self.run_activation([state])
                self.assertEqual(result["service"], state)
                self.assertEqual(result["status"], "incomplete")
        self.retire_service.assert_not_called()
        self.prepare_service.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_delayed_writer_cannot_prepare_against_a_newer_binding(self):
        release = self.root / "candidate"
        (release / "runtime.json").parent.mkdir(exist_ok=True)
        (release / "runtime.json").write_text(json.dumps(
            dict(python=sys.executable, validation=VALIDATION)), encoding="utf-8")
        expected = dict(runtime_binding(self.old), engine_config=str(self.root / "newer.json"))
        with self.assertRaises(release_activation.InstallationChanged):
            release_activation.prepare(self.config, release, expected_binding=expected)
        self.assertEqual(self.config.read_bytes(), self.original)
        digest = __import__("hashlib").sha256(
            str(self.config.absolute()).encode()).hexdigest()
        self.assertFalse((release / ("profile-" + digest + ".engine.json")).exists())

    def test_persisted_intent_reconciles_already_published_target_without_rewrite(self):
        release = self.root / "candidate"
        release.mkdir()
        (release / "runtime.json").write_text(json.dumps(
            dict(python=sys.executable, validation=VALIDATION)), encoding="utf-8")
        digest = __import__("hashlib").sha256(
            str(self.config.absolute()).encode()).hexdigest()
        engine = release / ("profile-" + digest + ".engine.json")
        engine.write_text("{}", encoding="utf-8")
        target = dict(self.old, python=sys.executable,
                      runtime_scripts=str(SCRIPTS), engine_config=str(engine),
                      product_validation=VALIDATION, component_release=None)
        self.config.write_text(json.dumps(target), encoding="utf-8")
        before = self.config.read_bytes()
        attempt = io.BytesIO(json.dumps({
            "schema": release_activation.ATTEMPT_SCHEMA,
            "expected_binding": runtime_binding(self.old)}).encode())
        with patch("mindie_knowledge.loop.handoff._probe", return_value="absent"):
            result = release_activation.main(
                ["release_activation.py", str(self.config), str(release), "--attempt"],
                stdin=attempt)
        self.assertEqual(result["status"], "active")
        self.assertEqual(self.config.read_bytes(), before)
        self.retire_service.assert_not_called()
        self.prepare_service.assert_not_called()

    def test_untagged_legacy_path_argument_is_rejected(self):
        release = self.root / "candidate"
        release.mkdir()
        (release / "runtime.json").write_text(json.dumps(
            dict(python=sys.executable, validation=VALIDATION)), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "--attempt"):
            release_activation.main(
                ["release_activation.py", str(self.config), str(release),
                 str(self.root / "native.json")])

    def test_persisted_intent_refuses_a_third_canonical_binding(self):
        release = self.root / "candidate"
        release.mkdir()
        (release / "runtime.json").write_text(json.dumps(
            dict(python=sys.executable, validation=VALIDATION)), encoding="utf-8")
        third = dict(self.old, python="/third/python")
        self.config.write_text(json.dumps(third), encoding="utf-8")
        before = self.config.read_bytes()
        attempt = io.BytesIO(json.dumps({
            "schema": release_activation.ATTEMPT_SCHEMA,
            "expected_binding": runtime_binding(self.old)}).encode())
        result = release_activation.main(
            ["release_activation.py", str(self.config), str(release), "--attempt"],
            stdin=attempt)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "installation_changed")
        self.assertEqual(self.config.read_bytes(), before)

    def test_competing_binding_never_retires_or_overwrites(self):
        self.config.write_text(json.dumps(dict(self.old, python="/third/python")))
        before = self.config.read_bytes()
        result = self.run_activation([])
        self.assertEqual(result["reason"], "installation_changed")
        self.retire_service.assert_not_called()
        self.prepare_service.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_control_expiry_does_not_kill_candidate_or_publish(self):
        with patch("mindie_knowledge.loop.handoff._probe", return_value="starting"):
            self.inspect_retirement.return_value = {"status": "retired"}
            result = release_activation.activate(self.config, self.prepared, control_seconds=.001)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.prepare_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
