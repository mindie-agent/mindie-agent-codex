"""Exact service ownership and cold-start provider context; no version routing."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import service_client  # noqa: E402
from mindie_knowledge.loop import cli, handoff, lifecycle  # noqa: E402


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

    def test_default_authority_needs_no_helper_but_overrides_and_unknowns_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Path(directory) / "engine.json"
            engine.write_text(json.dumps({"domain": "test"}), encoding="utf-8")
            with patch.dict(sys.modules, {"mindie_knowledge.loop.domain_routes": None}):
                self.assertEqual(
                    service_client.select_engine(str(engine), ref="mindie://test/units/x"),
                    str(engine))
                self.assertEqual(
                    service_client.select_engine(str(engine), domain="test"),
                    str(engine))
                with self.assertRaisesRegex(service_client.DomainRouteError, "does not provide"):
                    service_client.select_engine(
                        str(engine), ref="mindie://vllm-ascend/units/x")
                with self.assertRaisesRegex(service_client.DomainRouteError, "does not provide"):
                    service_client.select_engine(
                        str(engine), domain="vllm-ascend")
                with self.assertRaisesRegex(service_client.DomainRouteError, "conflicts"):
                    service_client.select_engine(
                        str(engine), domain="vllm-ascend",
                        ref="mindie://test/units/x")
                # A trusted mapping override is never silently bypassed.
                with self.assertRaisesRegex(service_client.DomainRouteError, "does not provide"):
                    service_client.select_engine(
                        str(engine), domain="test",
                        domain_configs={"test": "/mapped/engine.json"})

    def test_batch_refs_use_first_canonical_authority_without_mixing(self):
        first = "mindie://test/units/a@1"
        self.assertEqual(
            service_client.route_ref(refs=[first, "mindie://test/units/b@2"]),
            first)
        self.assertIsNone(service_client.route_ref(refs=["unit-without-authority"]))
        with self.assertRaisesRegex(service_client.DomainRouteError, "multiple domain"):
            service_client.route_ref(
                refs=[first, "mindie://vllm-ascend/units/b@2"])

        with tempfile.TemporaryDirectory() as directory:
            engine = Path(directory) / "engine.json"
            engine.write_text(json.dumps({"domain": "test"}), encoding="utf-8")
            self.assertEqual(
                service_client.select_engine(
                    str(engine), refs=[first, "mindie://test/units/b@2"]),
                str(engine))

    def test_internal_helper_import_failure_is_not_misreported_as_old_runtime(self):
        import builtins

        real_import = builtins.__import__

        def broken(name, *args, **kwargs):
            if name == "mindie_knowledge.loop.domain_routes":
                error = ModuleNotFoundError("No module named 'internal_dependency'")
                error.name = "internal_dependency"
                raise error
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=broken), \
                self.assertRaises(ModuleNotFoundError) as caught:
            service_client.select_engine("/exact.json", domain="vllm-ascend")
        self.assertEqual(caught.exception.name, "internal_dependency")

    def test_retired_configuration_never_starts_an_alternative_owner(self):
        with patch.object(cli, "config_at", return_value={}), \
                patch.object(lifecycle, "require_active", side_effect=ValueError("retired")), \
                patch.object(cli, "ensure_service") as start:
            with self.assertRaisesRegex(ValueError, "retired"):
                service_client.connect("/exact.json")
            start.assert_not_called()
