"""Exact service ownership and cold-start provider context; no version routing."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import service_client
from mindie_knowledge.loop import cli, handoff, lifecycle


class ClientTests(unittest.TestCase):
    def test_live_exact_owner_does_not_require_resupplying_provider_secret(self):
        ready = {"config_fingerprint": "exact", "token": "private"}
        with patch.object(cli, "config_at", return_value={}), \
                patch.object(lifecycle, "require_active"), \
                patch.object(handoff, "_probe", return_value="ready"), \
                patch.object(cli, "connect", return_value=ready) as connection, \
                patch("summary_provider.required_environment", side_effect=AssertionError("cold only")):
            self.assertIs(service_client.connect("/exact.json"), ready)
            connection.assert_called_once_with({}, config_path="/exact.json")

    def test_cold_missing_selected_context_prevents_spawn(self):
        with patch.object(cli, "config_at", return_value={}), \
                patch.object(lifecycle, "require_active"), \
                patch.object(handoff, "_probe", return_value="absent"), \
                patch("summary_provider.required_environment", side_effect=ValueError("required context unavailable")), \
                patch.object(cli, "ensure_service") as start:
            with self.assertRaisesRegex(ValueError, "context unavailable"):
                service_client.connect("/exact.json")
            start.assert_not_called()

    def test_retired_configuration_never_starts_an_alternative_owner(self):
        with patch.object(cli, "config_at", return_value={}), \
                patch.object(lifecycle, "require_active", side_effect=ValueError("retired")), \
                patch.object(cli, "ensure_service") as start:
            with self.assertRaisesRegex(ValueError, "retired"):
                service_client.connect("/exact.json")
            start.assert_not_called()
