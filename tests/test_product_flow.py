"""Product outcomes, orthogonal to protocol/ownership component checks.

Only the external service attachment is fault-injected. Real configuration,
consent, native-task binding and status paths run; this is not native E2E proof.
"""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from tests.test_sharing import SharingFixture, SCRIPTS
import bridge
import consent
import sharing


class ProductFlowTests(SharingFixture):
    def test_removed_modes_cannot_complete_new_setup(self):
        for word in ('read-only', 'later'):
            with self.assertRaises(ValueError):
                bridge.sharing_operation('sharing-choice', word)
        self.assertIsNone(consent.load()['choice'])

    def enter(self):
        with patch.object(Path, "cwd", return_value=self.scope):
            return bridge.activate("activate")

    def test_installed_but_unconfigured_is_not_a_successful_product_mode(self):
        with patch.object(bridge, "bind") as attach:
            result = self.enter()
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["experience"], "needs-configuration")
        self.assertIn("repository", result["next"])
        attach.assert_not_called()

    def test_saved_contribution_does_not_hide_missing_configuration(self):
        consent.record_choice("contribute")
        self.assertEqual(self.enter()["experience"], "needs-configuration")

    def test_legacy_decline_is_preserved_without_enabling_capture(self):
        self.write_sharing()
        for choice in ("read-only", "later", "disabled"):
            with self.subTest(choice=choice), patch.object(bridge, "bind") as attach:
                consent.record_choice(choice)
                result = self.enter()
                self.assertEqual(result["experience"], "disabled")
                self.assertEqual(consent.load()["choice"], choice)
                attach.assert_not_called()

    def test_scope_and_service_failure_are_not_capture_readiness(self):
        self.write_sharing(project_roots=[str(self.root / "other")])
        consent.record_choice("contribute")
        with patch.object(bridge, "bind") as attach:
            self.assertEqual(self.enter()["experience"], "out-of-scope")
            attach.assert_not_called()
        self.write_sharing()
        with patch.object(bridge, "bind", return_value="unbound:service-error"):
            self.assertEqual(self.enter()["experience"], "unavailable")

    def test_configure_attaches_the_existing_task_without_second_activation(self):
        first = self.enter()
        with patch.object(bridge, "bind", return_value="bound") as attach:
            result = bridge.configure([
                "--community-repository", "owner/knowledge",
                "--community-project-root", str(self.scope),
                "--community-visibility", "public",
            ])
        attach.assert_called_once()
        activated = result["activation"]
        self.assertEqual(activated["experience"], "capture-ready")
        self.assertEqual(activated["mindie_activation"], first["mindie_activation"])

    def test_failed_configuration_does_not_record_completion(self):
        self.community.write_text("{broken", encoding="utf-8")
        before = consent.load()["choice"]
        result = subprocess.run([
            sys.executable, str(SCRIPTS / "setup.py"), "configure",
            "--config", str(self.config), "--community-repository", "owner/knowledge",
            "--community-project-root", str(self.scope), "--community-visibility", "public",
        ], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(consent.load()["choice"], before)
        self.assertEqual(self.community.read_text(encoding="utf-8"), "{broken")
