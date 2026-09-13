import unittest

from kagebunshin.knowledge.application.library import Library
from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.domain.contracts import InvalidContract, Node, Plan
from kagebunshin.orchestration.ports import Result
from tests.unit.test_knowledge import MemoryKnowledge
from tests.unit.fakes import MemoryStore, ROLES, ScriptedProvider


class EvidenceHandoffTest(unittest.TestCase):
    def setUp(self):
        self.library = Library(MemoryKnowledge())
        self.sid = self.library.register(title="fixture", url="local:fixture", content="owned deletion marker",
                                         coverage="all", rights="owned", accessed_at="2026-09-13")
        self.cid = self.library.claim("owned deletion marker", [self.sid])
        self.refs = {self.sid: self.library.status()["sources"][self.sid]["content_hash"]}

    def test_prepared_review_sends_nothing_and_retraction_fences_it(self):
        provider = ScriptedProvider()
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000, self.library)
        run_id = engine.submit_claim_review(self.cid, self.library.bundle([self.cid]))
        run = engine.status(run_id)
        self.assertEqual(provider.requests, [])
        self.assertEqual(run["evidence_refs"], self.refs)
        self.assertEqual(run["plan"]["nodes"][0]["audit_role"], "R12")
        self.assertEqual(run["plan"]["nodes"][0]["role_id"], "R07")
        self.assertIn("ClaimUnderReview", run["plan"]["nodes"][0]["source_ids"])
        self.library.retract(self.sid)
        self.assertEqual(engine.run_until_idle(run_id)["state"], "blocked_contract_changed")
        self.assertEqual(provider.requests, [])

    def test_retraction_fences_pending_job_before_provider_io(self):
        provider = ScriptedProvider()
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000, self.library)
        run = engine.submit_evidence("summarize", self.library.bundle([self.cid]))
        self.library.retract(self.sid)
        engine.run_until_idle(run)
        self.assertEqual(provider.requests, [])
        self.assertEqual(engine.status(run)["state"], "blocked_contract_changed")
        with self.assertRaises(InvalidContract):
            engine.create(Plan("a", (Node("a", "R07", "a"),), {}), evidence_refs=self.refs)

    def test_erase_keeps_unknown_slot_and_discards_late_result_text(self):
        provider = ScriptedProvider(lambda request: Result("unknown", thread_id="owned-thread", turn_id="owned-turn"))
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000, self.library)
        run = engine.create(Plan("owned deletion marker", (Node("a", "R07", "owned deletion marker"),), {}), evidence_refs=self.refs)
        engine.step(run)
        self.library.erase(self.sid)
        receipt = engine.erase_evidence(self.sid)
        self.assertEqual(receipt["jobs"][0]["unknown_dispatches"], 1)
        self.assertEqual(receipt["jobs"][0]["provider_threads_pending"], ["owned-thread"])
        self.assertIsNotNone(engine.status(run)["nodes"]["a"]["active"])
        self.assertNotIn("owned deletion marker", str(engine.status(run)))
        provider.reconciled = Result("completed", {"text": "owned deletion marker"})
        engine.reconcile(run)
        self.assertNotIn("owned deletion marker", str(engine.status(run)))
        self.assertIsNone(engine.status(run)["nodes"]["a"]["active"])
        with self.assertRaises(InvalidContract): engine.resume(run)
        self.assertEqual(engine.erase_evidence(self.sid)["jobs"][0]["run_id"], run)
