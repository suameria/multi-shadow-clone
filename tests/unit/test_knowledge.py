from copy import deepcopy
import unittest

from multi_shadow_clone.knowledge.application.library import Library
from multi_shadow_clone.knowledge.domain.model import InvalidEvidence
from multi_shadow_clone.knowledge.ports import Conflict


class MemoryKnowledge:
    def __init__(self):
        self.book = {"revision": 0, "sources": {}, "claims": {}, "relations": {}, "consumers": {}, "erasures": {}}

    def read(self):
        return deepcopy(self.book)

    def save(self, book, expected_revision):
        if self.book["revision"] != expected_revision:
            raise Conflict()
        self.book = deepcopy({**book, "revision": expected_revision + 1})


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryKnowledge()
        self.library = Library(self.store)

    def source(self, text="synthetic original", date="2026-09-01"):
        return self.library.register(title="fixture", url="https://example.test/source", content=text,
                                     coverage="complete fixture", rights="test-owned", accessed_at=date)

    def test_newer_source_keeps_original_and_conflicting_claims(self):
        first = self.source("A measured 4", "2026-08-01")
        later = self.source("A measured 9", "2026-09-01")
        a, b = self.library.claim("A is 4", [first]), self.library.claim("A is 9", [later])
        self.library.relate(b, a, "contradicts", "different observations")
        bundle = self.library.bundle([b])
        self.assertEqual({c["id"] for c in bundle["claims"]}, {a, b})
        self.assertEqual({c["state"] for c in bundle["claims"]}, {"candidate"})
        with self.assertRaises(InvalidEvidence):
            self.library.relate(b, a, "supersedes", "newer is not proof")

    def test_independent_review_is_required_and_bound_to_exact_claim(self):
        sid = self.source()
        claim = self.library.claim("a statement", [sid])
        with self.assertRaises(InvalidEvidence):
            self.library.support(claim, {"approved": True})
        class Review:
            def verifies(self, candidate, reference):
                return reference == {"claim_hash": candidate["hash"]}
        self.library.review = Review()
        with self.assertRaises(InvalidEvidence):
            self.library.support(claim, {"claim_hash": "old"})
        self.library.support(claim, {"claim_hash": self.store.book["claims"][claim]["hash"]})
        self.assertEqual(self.library.bundle([claim])["claims"][0]["state"], "supported")

    def test_unknown_retracted_or_changed_evidence_cannot_be_materialized(self):
        with self.assertRaises(InvalidEvidence):
            self.library.claim("unsupported", ["missing"])
        sid = self.source()
        cid = self.library.claim("statement", [sid])
        self.store.book["sources"][sid]["content"] = "changed"
        with self.assertRaises(InvalidEvidence):
            self.library.bundle([cid])
        sid2 = self.source()
        cid2 = self.library.claim("statement", [sid2])
        self.library.retract(sid2)
        with self.assertRaises(InvalidEvidence):
            self.library.bundle([cid2])

    def test_directed_cycles_are_rejected_but_contradictions_are_kept(self):
        sid = self.source()
        a, b, c = [self.library.claim(s, [sid]) for s in ("a", "b", "c")]
        self.library.relate(a, b, "supports", "synthetic relationship")
        self.library.relate(b, c, "supports", "synthetic relationship")
        with self.assertRaises(InvalidEvidence):
            self.library.relate(c, a, "supports", "cycle")
        self.library.relate(c, a, "contradicts", "counterevidence")
        self.assertEqual(len(self.library.bundle([a])["relations"]), 3)

    def test_erasure_removes_local_derivatives_and_reports_external_consumers(self):
        sid = self.source("owned-secret-marker")
        a, b = [self.library.claim("owned-secret-marker " + s, [sid]) for s in ("a", "b")]
        self.library.relate(a, b, "contradicts", "owned-secret-marker")
        self.library.register_consumer([sid], "codex-thread:owned-fixture")
        receipt = self.library.erase(sid)
        self.assertNotIn("owned-secret-marker", str(self.library.status()))
        self.assertEqual(receipt["consumers_requiring_erasure"], ["codex-thread:owned-fixture"])
        self.assertEqual(self.library.erase(sid), receipt)
        with self.assertRaises(InvalidEvidence):
            self.library.bundle([a])
