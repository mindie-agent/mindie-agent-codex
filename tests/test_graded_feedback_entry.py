"""Native Codex MCP feedback on exact observed graded-unit revisions.

The unused native model executable is inherited from the existing C6 route
fixture. The MCP gate, admission lease, knowledge Service, Engine, Store and
graded catalog are the real shared sources.
"""

import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from tests.test_mcp_knowledge_distill import EVIDENCE, ServiceCase  # noqa: E402
from tests.process_fixtures import (  # noqa: E402
    SCRIPTS, public_engine_config, stop_owned_knowledge_service,
)

import mcp_gate  # noqa: E402
import runtime_call  # noqa: E402
import service_client  # noqa: E402
from mindie_knowledge.graded.units import GradedUnit  # noqa: E402
from mindie_knowledge.loop.store import Store, session_key  # noqa: E402


UNIT_ID = "unit-graded-feedback-entry"
SESSION = "thread-distill-1"
JOINT_BRIDGE = TESTS / "fixtures" / "joint_bridge.py"


def graded_unit(revision=1, body="Fixture graded feedback rule for the observed revision.\n",
                domain_path="ops/graded-feedback"):
    return GradedUnit.from_parts({
        "schema": "mindie-graded-unit/1",
        "id": UNIT_ID,
        "tier": "L2",
        "category": "sop",
        "evidence_strength": "single_observation",
        "domain_path": domain_path,
        "revision": revision,
        "inherits_from": [],
        "title": "Synthetic graded feedback entry",
        "triggers": {
            "error_signatures": ["SYNTHETIC_GRADED_FEEDBACK"],
            "keywords": ["gradedfeedbackprobe"],
        },
        "conditions": {"soc": ["Ascend910B"]},
        "provenance": {
            "source_type": "session_distill",
            "source_locator": "fixture://graded-feedback-entry",
            "source_digest": "c" * 64,
            "evidence_refs": ["fixture-observation"],
            "supersedes": [],
        },
    }, body, f"{domain_path}/{UNIT_ID}.md")


class GradedFeedbackEntryTests(ServiceCase):
    def _install(self, unit):
        store = Store(self.data_root, self.domain)
        try:
            store.graded.install([unit])
        finally:
            store.close()

    def _query(self, gate, request_id):
        result = gate.call(self._request(request_id, "knowledge_query", {
            "query": "gradedfeedbackprobe",
            "conditions": {"soc": "Ascend910B"},
            "limit": 5,
        }))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]["results"][0]

    def _feedback(self, gate, request_id, ref, rating, reason):
        result = gate.call(self._request(request_id, "knowledge_feedback", {
            "ref": ref, "rating": rating, "reason": reason,
        }))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _votes(self):
        store = Store(self.data_root, self.domain)
        try:
            rows = [dict(row) for row in store.db.execute("SELECT * FROM votes")]
            root = store.db.execute(
                "SELECT opaque FROM opaque_roots WHERE root_hash=?",
                (session_key(SESSION),),
            ).fetchone()
            return rows, (root["opaque"] if root else None)
        finally:
            store.close()

    def test_query_feedback_records_exact_observed_revision_and_native_session(self):
        unit = graded_unit()
        self._install(unit)
        self._activate()
        gate = mcp_gate.Gate("knowledge")

        mismatch = gate.call(self._request(99, "knowledge_query", {
            "query": "gradedfeedbackprobe", "conditions": {"soc": "Ascend910C"},
        }))
        self.assertFalse(mismatch["isError"], mismatch)
        self.assertEqual(mismatch["structuredContent"]["results"], [])
        hit = self._query(gate, 100)
        observed = f"mindie://{self.domain}/units/{UNIT_ID}@{unit.revision}"
        self.assertEqual(hit["feedback_ref"], observed)
        self.assertEqual(hit["revision"], 1)

        vote = self._feedback(gate, 101, hit["feedback_ref"], "up",
                              "Confirmed during the fixture run.")
        self.assertEqual(vote["unit_id"], UNIT_ID)
        self.assertEqual(vote["revision"], 1)
        self.assertEqual(vote["rating"], "up")

        rows, opaque = self._votes()
        self.assertIsNotNone(opaque)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_id"], UNIT_ID)
        self.assertEqual(int(rows[0]["revision"]), unit.revision)
        self.assertEqual(rows[0]["root_opaque"], opaque)

    def test_stale_ref_keeps_the_observed_revision_after_the_unit_is_revised(self):
        original = graded_unit()
        self._install(original)
        self._activate()
        gate = mcp_gate.Gate("knowledge")

        first = self._query(gate, 110)
        revised = graded_unit(
            2, "Fixture graded feedback rule corrected for the next revision.\n")
        self._install(revised)
        second = self._query(gate, 111)
        self.assertNotEqual(first["feedback_ref"], second["feedback_ref"])
        self.assertEqual(
            second["feedback_ref"],
            f"mindie://{self.domain}/units/{UNIT_ID}@{revised.revision}",
        )

        old_vote = self._feedback(gate, 112, first["feedback_ref"], "down",
                                  "The first revision failed.")
        new_vote = self._feedback(gate, 113, second["feedback_ref"], "up",
                                  "The correction worked.")
        self.assertEqual(old_vote["revision"], 1)
        self.assertEqual(new_vote["revision"], 2)

        rows, _ = self._votes()
        by_revision = {int(row["revision"]): row for row in rows}
        self.assertEqual(set(by_revision), {1, 2})
        self.assertEqual(by_revision[1]["rating"], "down")
        self.assertEqual(by_revision[2]["rating"], "up")

class MultiDomainEntryTests(ServiceCase):
    """One native entry, two existing engines, no federated fallback."""

    domain = "operators"
    target_domain = "vllm-ascend"

    def setUp(self):
        super().setUp()
        self.target_data_root = self.root / "data-vllm-ascend"
        self.target_engine = self.root / "vllm-ascend.engine.json"
        self.target_community = self.root / "vllm-ascend-community.json"
        self.target_admission = self.root / "vllm-ascend.admission.sqlite3"
        config = public_engine_config(
            self.target_data_root, domain=self.target_domain,
            adapter_profile=self.config, admission_path=str(self.target_admission),
            community_config=str(self.target_community))
        config["summary_command"] = [sys.executable, str(SCRIPTS / "agent_worker.py")]
        config["service_launch_context"]["paths"]["MINDIE_CODEX_BIN"] = str(self.fake_codex)
        self.target_engine.write_text(json.dumps(config), encoding="utf-8")
        self.target_community.write_text(json.dumps(dict(
            schema="mindie-community-config/1", enabled=False, generation=None,
            enabled_at=None, repository=None, branch="main", project_roots=[],
            idle_seconds=300, consent_config=str(self.root / "vllm-ascend-consent.json"),
        )), encoding="utf-8")
        adapter = json.loads(self.config.read_text(encoding="utf-8"))
        adapter["domain_configs"] = {self.target_domain: str(self.target_engine)}
        self.config.write_text(json.dumps(adapter), encoding="utf-8")

    def tearDown(self):
        # The target engine is its own owned service; stop it before the
        # shared fixture removes the temporary directory.
        try:
            stop_owned_knowledge_service(self.target_engine)
        finally:
            super().tearDown()

    def _install_in(self, data_root, domain, unit):
        store = Store(data_root, domain)
        try:
            store.graded.install([unit])
        finally:
            store.close()

    def _query(self, gate, request_id, *, domain=None):
        arguments = {
            "query": "gradedfeedbackprobe",
            "conditions": {"soc": "Ascend910B"},
            "limit": 5,
        }
        if domain is not None:
            arguments["domain"] = domain
        result = gate.call(self._request(request_id, "knowledge_query", arguments))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]["results"][0]

    def _votes_in(self, data_root, domain):
        store = Store(data_root, domain)
        try:
            return [dict(row) for row in store.db.execute("SELECT * FROM votes")]
        finally:
            store.close()

    def _graded_ids(self, data_root, domain):
        store = Store(data_root, domain)
        try:
            if not store.graded.available():
                return set()
            return set(store.graded._load().units)
        finally:
            store.close()

    def test_explicit_query_and_uri_reads_select_independent_domain_stores(self):
        default = graded_unit(
            1, "Operators repository rule for the observed revision.\n")
        target = graded_unit(
            2, "vllm-ascend repository rule for its own observed revision.\n",
            domain_path="ops/vllm-ascend")
        self._install_in(self.data_root, self.domain, default)
        self._install_in(self.target_data_root, self.target_domain, target)
        self._activate()
        gate = mcp_gate.Gate("knowledge")

        # No domain keeps the installed default behavior.
        default_hit = self._query(gate, 1000)
        self.assertEqual(default_hit["revision"], 1)
        self.assertIn("Operators repository", default_hit["excerpt"])
        self.assertEqual(default_hit["feedback_ref"],
                         f"mindie://{self.domain}/units/{UNIT_ID}@{default.revision}")

        # Explicit domain selects only the configured target store; same
        # unit_id is a different unit with a different revision.
        target_hit = self._query(gate, 1001, domain=self.target_domain)
        self.assertEqual(target_hit["revision"], 2)
        self.assertIn("vllm-ascend repository", target_hit["excerpt"])
        self.assertEqual(target_hit["feedback_ref"],
                         f"mindie://{self.target_domain}/units/{UNIT_ID}@{target.revision}")
        self.assertNotEqual(default_hit["feedback_ref"], target_hit["feedback_ref"])

        # The returned URI, not a caller path/map, selects the read engine.
        explained = gate.call(self._request(
            1002, "knowledge_explain", {"ref": target_hit["ref"]}))
        self.assertFalse(explained["isError"], explained)
        value = explained["structuredContent"]
        self.assertEqual(value["domain"], self.target_domain)
        self.assertEqual(value["revision"], 2)
        self.assertIn("vllm-ascend repository", value["content"])
        # Ordinary knowledge reads never activate the target lease.
        self.assertFalse(self.target_admission.exists())

        unknown = gate.call(self._request(1003, "knowledge_query", {
            "query": "gradedfeedbackprobe", "domain": "not-configured"}))
        self.assertTrue(unknown["isError"])
        self.assertEqual(unknown["structuredContent"]["code"], "domain_routing_rejected")
        self.assertIn("unknown domain", unknown["structuredContent"]["message"])

        # A configured domain whose engine declares another domain is a
        # visible mismatch, never a default fallback.
        adapter = json.loads(self.config.read_text(encoding="utf-8"))
        adapter["domain_configs"] = {self.domain: str(self.target_engine)}
        self.config.write_text(json.dumps(adapter), encoding="utf-8")
        mismatch = gate.call(self._request(1004, "knowledge_query", {
            "query": "gradedfeedbackprobe", "domain": self.domain}))
        self.assertTrue(mismatch["isError"])
        self.assertEqual(mismatch["structuredContent"]["code"], "domain_routing_rejected")
        self.assertIn("declares", mismatch["structuredContent"]["message"])
        self.assertIn(self.target_domain, mismatch["structuredContent"]["message"])

        # A direct explicit-domain/reference conflict is never resolved by
        # preferring one side silently.
        with self.assertRaisesRegex(service_client.DomainRouteError, "conflicts"):
            runtime_call.call(dict(
                surface="knowledge", name="knowledge_query",
                arguments={
                    "query": "gradedfeedbackprobe", "domain": self.target_domain,
                    "ref": f"mindie://{self.domain}/units/{UNIT_ID}",
                },
                mindie_session_id=SESSION, native_session_verified=True))

    def test_default_uri_reads_through_the_default_engine_without_the_new_helper(self):
        self._activate()
        before = Path(self.admission).read_bytes()
        payload = dict(
            surface="knowledge", name="knowledge_explain",
            arguments={"ref": f"mindie://{self.domain}/units/{UNIT_ID}"},
            mindie_session_id=SESSION, native_session_verified=True,
        )
        with patch.dict(sys.modules, {"mindie_knowledge.loop.domain_routes": None}), \
                patch("service_client.connect", return_value={}) as connect, \
                patch("mindie_knowledge.loop.transport.rpc",
                      return_value={"kind": "graded_unit", "domain": self.domain}) as rpc:
            result = runtime_call.call(payload)
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["domain"], self.domain)
        connect.assert_called_once_with(str(self.engine))
        self.assertEqual(rpc.call_args.args[1], "explain")
        self.assertEqual(Path(self.admission).read_bytes(), before)

        with patch.dict(sys.modules, {"mindie_knowledge.loop.domain_routes": None}), \
                self.assertRaisesRegex(service_client.DomainRouteError, "does not provide"):
            runtime_call.call(dict(
                payload, arguments={
                    "ref": f"mindie://{self.target_domain}/units/{UNIT_ID}",
                }))

    def test_uri_feedback_honors_the_target_engines_own_consent(self):
        target = graded_unit(
            2, "vllm-ascend feedback target rule.\n", domain_path="ops/vllm-ascend")
        self._install_in(self.target_data_root, self.target_domain, target)
        self._activate()
        # Default-domain sharing is enabled; the target stays disabled. The
        # target vote must remain local rather than inherit that authority.
        self._enable_sharing()
        gate = mcp_gate.Gate("knowledge")
        hit = self._query(gate, 1100, domain=self.target_domain)
        explained = gate.call(self._request(
            1101, "knowledge_explain", {"ref": hit["ref"]}))
        self.assertFalse(explained["isError"], explained)
        page = explained["structuredContent"]
        self.assertEqual(page["feedback_ref"], hit["ref"])

        result = gate.call(self._request(1102, "knowledge_feedback", {
            "ref": page["feedback_ref"], "rating": "up",
            "reason": "Confirmed only in the target repository fixture."}))
        self.assertFalse(result["isError"], result)
        vote = result["structuredContent"]
        self.assertEqual(vote["unit_id"], UNIT_ID)
        self.assertEqual(vote["revision"], 2)
        self.assertFalse(vote["publishable"])

        rows = self._votes_in(self.target_data_root, self.target_domain)
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["revision"]), target.revision)
        self.assertFalse(bool(rows[0]["publishable"]))
        self.assertEqual(self._votes_in(self.data_root, self.domain), [])
        self.assertFalse(self.target_admission.exists())

    def test_distill_missing_target_lease_fails_before_the_model(self):
        self._activate()
        self._enable_sharing()
        gate = mcp_gate.Gate("knowledge")
        result = gate.call(self._request(1200, "knowledge_distill", {
            "inputs": EVIDENCE,
            "task_state": "verified_milestone",
            "domain": self.target_domain,
        }))
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["code"], "lease_required")
        self.assertIn("selected domain", result["structuredContent"]["message"])
        self.assertFalse(self.marker.exists())

    def test_native_activated_independent_target_lease_drives_distill(self):
        from mindie_knowledge import consent_store
        from mindie_knowledge.loop.activation import Admission

        target_consent = self.root / "vllm-ascend-consent.json"
        consent_store.record_choice(target_consent, "contribute")
        self.target_community.write_text(json.dumps(dict(
            schema="mindie-community-config/1", enabled=True,
            generation="target-generation", enabled_at=time.time(),
            repository="owner/vllm-ascend", branch="main",
            project_roots=[str(self.scope)], idle_seconds=300,
            consent_config=str(target_consent),
        )), encoding="utf-8")
        consent_store.record_choice(self.consent, "disabled")
        default_token = self._activate()["mindie_activation"]
        default_consent = consent_store.read(self.consent)
        self.assertFalse(self.target_admission.exists())

        activation = subprocess.run(
            [sys.executable, str(JOINT_BRIDGE), "activate",
             "--domain", self.target_domain],
            text=True,
            capture_output=True,
            timeout=30,
            cwd=str(self.scope),
        )
        self.assertEqual(activation.returncode, 0, activation.stderr)
        activated = json.loads(activation.stdout)
        self.assertEqual(activated["domain"], self.target_domain)
        self.assertEqual(activated["generation"], "target-generation")
        self.assertNotEqual(activated["mindie_activation"], default_token)
        target_lease = Admission(self.target_admission).active_lease(SESSION)
        self.assertIsNotNone(target_lease)
        self.assertEqual(target_lease["token"], activated["mindie_activation"])
        self.assertEqual(Admission(self.admission).active_lease(SESSION)["token"],
                         default_token)
        self.assertEqual(consent_store.read(self.consent), default_consent)

        gate = mcp_gate.Gate("knowledge")
        result = gate.call(self._request(1300, "knowledge_distill", {
            "inputs": EVIDENCE,
            "task_state": "verified_milestone",
            "domain": self.target_domain,
        }))
        self.assertFalse(result["isError"], result)
        value = result["structuredContent"]
        self.assertEqual(value["status"], "applied")
        self.assertIn("unit-mcp-distill-fixture", value["admitted_unit_ids"])
        self.assertTrue(self.marker.exists())
        self.assertIn("unit-mcp-distill-fixture",
                      self._graded_ids(self.target_data_root, self.target_domain))
        self.assertNotIn("unit-mcp-distill-fixture",
                         self._graded_ids(self.data_root, self.domain))
        self.assertEqual(Admission(self.admission).active_lease(SESSION)["token"],
                         default_token)
        self.assertEqual(consent_store.read(self.consent), default_consent)

    def test_distill_accepts_a_target_that_deliberately_shares_admission(self):
        from mindie_knowledge import consent_store

        target = graded_unit(
            2, "vllm-ascend shared-admission distill target.\n",
            domain_path="ops/vllm-ascend")
        self._install_in(self.target_data_root, self.target_domain, target)
        self._activate()
        # Deliberate shared admission is trusted existing configuration. The
        # target's own sharing/consent gate still decides whether work runs.
        target_consent = self.root / "vllm-ascend-consent.json"
        consent_store.record_choice(target_consent, "contribute")
        self.target_community.write_text(json.dumps(dict(
            schema="mindie-community-config/1", enabled=True,
            generation="target-generation", enabled_at=time.time(),
            repository="owner/vllm-ascend", branch="main",
            project_roots=[str(self.scope)], idle_seconds=300,
            consent_config=str(target_consent),
        )), encoding="utf-8")
        config = json.loads(self.target_engine.read_text(encoding="utf-8"))
        config["admission_path"] = str(self.admission)
        self.target_engine.write_text(json.dumps(config), encoding="utf-8")

        gate = mcp_gate.Gate("knowledge")
        result = gate.call(self._request(1201, "knowledge_distill", {
            "inputs": EVIDENCE,
            "task_state": "verified_milestone",
            "domain": self.target_domain,
        }))
        self.assertFalse(result["isError"], result)
        value = result["structuredContent"]
        self.assertEqual(value["status"], "applied")
        self.assertIn("unit-mcp-distill-fixture", value["admitted_unit_ids"])
        self.assertTrue(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
