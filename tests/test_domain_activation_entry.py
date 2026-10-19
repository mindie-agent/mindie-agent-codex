"""Real native activation entry selects one explicitly configured domain.

The default and target engine configs, consent authorities, settings and
Admission stores are independent real files. Activation crosses the native
bridge CLI, the fixed-runtime admission helper, the shared core domain selector
and the target Admission; no target lease is pre-seeded.
"""

import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

from tests.test_sharing import SharingFixture
from tests.process_fixtures import public_engine_config

JOINT_BRIDGE = Path(__file__).resolve().parent / "fixtures" / "joint_bridge.py"

import consent  # noqa: E402
from mindie_knowledge import consent_store  # noqa: E402
from mindie_knowledge.loop.activation import Admission  # noqa: E402


TARGET_DOMAIN = "vllm-ascend"
TARGET_GENERATION = "target-generation"


class DomainActivationEntryTests(SharingFixture):
    def setUp(self):
        super().setUp()
        self.target_engine = self.root / "vllm-ascend.engine.json"
        self.target_community = self.root / "vllm-ascend-community.json"
        self.target_consent = self.root / "vllm-ascend-consent.json"
        self.target_admission = self.root / "vllm-ascend.admission.sqlite3"
        self.target_data = self.root / "target-data"
        self.write_target_sharing(project_roots=[str(self.scope)])
        consent_store.record_choice(self.target_consent, "contribute")
        engine = public_engine_config(
            self.target_data, domain=TARGET_DOMAIN,
            adapter_profile=self.config,
            admission_path=str(self.target_admission),
            community_config=str(self.target_community),
        )
        self.target_engine.write_text(json.dumps(engine), encoding="utf-8")
        adapter = json.loads(self.config.read_text(encoding="utf-8"))
        adapter["domain_configs"] = {
            TARGET_DOMAIN: str(self.target_engine.resolve()),
        }
        self.config.write_text(json.dumps(adapter), encoding="utf-8")
        # The default authority keeps its own saved choice, independent of the
        # target's contribution consent.
        consent.record_choice("disabled", self.config)

    def write_target_sharing(self, *, project_roots):
        self.target_community.write_text(json.dumps(dict(
            schema="mindie-community-config/1",
            enabled=True,
            generation=TARGET_GENERATION,
            enabled_at=time.time(),
            repository="owner/vllm-ascend",
            branch="main",
            project_roots=project_roots,
            idle_seconds=300,
            visibility="public",
            consent_config=str(self.target_consent),
        )), encoding="utf-8")

    def _activate_domain(self, domain):
        return subprocess.run(
            [sys.executable, str(JOINT_BRIDGE), "activate", "--domain", domain],
            text=True,
            capture_output=True,
            timeout=30,
            cwd=str(self.scope),
        )

    def _deactivate(self):
        return subprocess.run(
            [sys.executable, str(JOINT_BRIDGE), "deactivate"],
            text=True,
            capture_output=True,
            timeout=30,
            cwd=str(self.scope),
        )

    def _default_lease(self):
        return Admission(self.admission).active_lease("manual-A")

    def _target_lease(self):
        return Admission(self.target_admission).active_lease("manual-A")

    def test_domain_activation_binds_only_target_and_preserves_default(self):
        default = self.activate()
        default_token = default["mindie_activation"]
        default_consent = consent.load(self.config)
        target_consent = self.target_consent.read_bytes()

        result = self._activate_domain(TARGET_DOMAIN)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["domain"], TARGET_DOMAIN)
        self.assertEqual(Path(payload["engine_config"]).resolve(),
                         self.target_engine.resolve())
        self.assertEqual(payload["generation"], TARGET_GENERATION)
        self.assertEqual(payload["mindie_session_id"], "manual-A")
        self.assertEqual(payload["project_root"], str(self.scope.resolve()))
        self.assertNotIn("capture", payload)
        self.assertNotIn("experience", payload)

        target = self._target_lease()
        self.assertIsNotNone(target)
        self.assertEqual(target["token"], payload["mindie_activation"])
        self.assertEqual(target["project_root"], str(self.scope.resolve()))
        self.assertNotEqual(payload["mindie_activation"], default_token)
        self.assertEqual(len(Admission(self.target_admission).leases()), 1)

        self.assertEqual(self._default_lease()["token"], default_token)
        self.assertEqual(consent.load(self.config), default_consent)
        self.assertEqual(self.target_consent.read_bytes(), target_consent)

    def test_repeated_same_scope_domain_activation_preserves_target_token(self):
        default_token = self.activate()["mindie_activation"]
        first = self._activate_domain(TARGET_DOMAIN)
        second = self._activate_domain(TARGET_DOMAIN)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_payload = json.loads(first.stdout)
        second_payload = json.loads(second.stdout)
        self.assertEqual(first_payload["mindie_activation"],
                         second_payload["mindie_activation"])
        self.assertEqual(first_payload["activated_at"],
                         second_payload["activated_at"])
        self.assertEqual(len(Admission(self.target_admission).leases()), 1)
        self.assertEqual(self._default_lease()["token"], default_token)

    def test_out_of_scope_target_refuses_without_default_fallback(self):
        default_token = self.activate()["mindie_activation"]
        other = self.root / "other"
        other.mkdir()
        self.write_target_sharing(project_roots=[str(other)])

        result = self._activate_domain(TARGET_DOMAIN)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside", result.stderr)
        self.assertFalse(self.target_admission.exists())
        self.assertEqual(self._default_lease()["token"], default_token)

    def test_unknown_domain_refuses_without_default_fallback(self):
        default_token = self.activate()["mindie_activation"]

        result = self._activate_domain("unknown-domain")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown domain", result.stderr)
        self.assertFalse(self.target_admission.exists())
        self.assertEqual(self._default_lease()["token"], default_token)

    def test_bare_deactivate_revokes_current_session_across_registered_domains(self):
        default = self.activate()
        self.assertFalse(self.target_admission.exists())
        target_activation = self._activate_domain(TARGET_DOMAIN)
        self.assertEqual(target_activation.returncode, 0, target_activation.stderr)
        target = self._target_lease()
        self.assertIsNotNone(target)
        self.assertNotEqual(target["token"], default["mindie_activation"])

        default_other = Admission(self.admission).activate(
            "other-session", project_root=str(self.scope))
        target_other = Admission(self.target_admission).activate(
            "other-session", project_root=str(self.scope))
        default_consent = consent.consent_path_for(self.config).read_bytes()
        target_consent = self.target_consent.read_bytes()

        result = self._deactivate()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "inactive")
        self.assertIs(payload["changed"], True)
        self.assertEqual(payload["session_id"], "manual-A")
        default_domain = json.loads(self.engine.read_text(encoding="utf-8"))["domain"]
        self.assertEqual(
            {item["domain"] for item in payload["domains"]},
            {default_domain, TARGET_DOMAIN},
        )
        self.assertTrue(all(item["status"] == "inactive"
                            for item in payload["domains"]))

        self.assertIsNone(self._default_lease())
        self.assertIsNone(self._target_lease())
        self.assertEqual(
            Admission(self.admission).active_lease("other-session")["token"],
            default_other["token"],
        )
        self.assertEqual(
            Admission(self.target_admission).active_lease("other-session")["token"],
            target_other["token"],
        )
        self.assertEqual(consent.consent_path_for(self.config).read_bytes(),
                         default_consent)
        self.assertEqual(self.target_consent.read_bytes(), target_consent)

    def test_partial_deactivate_keeps_failures_visible_and_exits_nonzero(self):
        self.activate()
        target_activation = self._activate_domain(TARGET_DOMAIN)
        self.assertEqual(target_activation.returncode, 0, target_activation.stderr)
        target = self._target_lease()
        default_domain = json.loads(self.engine.read_text(encoding="utf-8"))["domain"]
        default_consent = consent.consent_path_for(self.config).read_bytes()
        target_config = self.target_engine.read_bytes()
        target_consent = self.target_consent.read_bytes()
        try:
            self.target_engine.write_text("{broken", encoding="utf-8")
            result = self._deactivate()
        finally:
            self.target_engine.write_bytes(target_config)

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "partial")
        self.assertIs(payload["changed"], True)
        self.assertEqual(payload["session_id"], "manual-A")
        outcomes = {item["domain"]: item for item in payload["domains"]}
        self.assertEqual(outcomes[default_domain]["status"], "inactive")
        self.assertEqual(outcomes[TARGET_DOMAIN]["status"], "unavailable")
        self.assertEqual(outcomes[TARGET_DOMAIN]["error_class"], "ValueError")
        self.assertIsNone(self._default_lease())
        self.assertEqual(self._target_lease()["token"], target["token"])
        self.assertEqual(consent.consent_path_for(self.config).read_bytes(),
                         default_consent)
        self.assertEqual(self.target_consent.read_bytes(), target_consent)

    def test_malformed_domain_configs_deactivate_fails_without_default_revoke(self):
        default = self.activate()
        adapter = json.loads(self.config.read_text(encoding="utf-8"))
        original = adapter["domain_configs"]
        try:
            adapter["domain_configs"] = ["not-a-mapping"]
            self.config.write_text(json.dumps(adapter), encoding="utf-8")
            result = self._deactivate()
        finally:
            adapter["domain_configs"] = original
            self.config.write_text(json.dumps(adapter), encoding="utf-8")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("domain_configs must be a mapping", result.stderr)
        self.assertEqual(self._default_lease()["token"],
                         default["mindie_activation"])


if __name__ == "__main__":
    unittest.main()
