"""Versioned registration, evidence selection, review and scoped erasure."""

from copy import deepcopy
from uuid import uuid4

from ..domain.model import InvalidEvidence, active_claim, active_source, fingerprint, nonempty, relation_allowed
from ..ports import ClaimReview, Conflict, KnowledgeStore


class Library:
    def __init__(self, store: KnowledgeStore, review: ClaimReview | None = None):
        self.store, self.review = store, review

    def _change(self, operation):
        while True:
            book = self.store.read()
            value = operation(book)
            if len(str(book)) > 20_000_000:
                raise InvalidEvidence("local library bound exceeded")
            try:
                self.store.save(book, book["revision"])
                return value
            except Conflict:
                continue

    def register(self, *, title: str, url: str, content: str, coverage: str,
                 rights: str, accessed_at: str) -> str:
        # This entry registers caller-supplied snapshots. It never fetches a URL.
        data = {k: nonempty(v) for k, v in locals().copy().items()
                if k in {"title", "url", "content", "coverage", "rights", "accessed_at"}}
        source_id = "S" + uuid4().hex
        def add(book):
            book["sources"][source_id] = {**data, "id": source_id, "state": "active",
                                            "content_hash": fingerprint(content)}
            return source_id
        return self._change(add)

    def claim(self, statement: str, source_ids: list[str]) -> str:
        nonempty(statement)
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise InvalidEvidence("a claim needs unique source references")
        claim_id = "C" + uuid4().hex
        def add(book):
            evidence = {sid: active_source(book, sid)["content_hash"] for sid in source_ids}
            body = {"statement": statement, "evidence": evidence}
            book["claims"][claim_id] = {**body, "id": claim_id, "hash": fingerprint(body),
                                          "state": "candidate", "review": None}
            return claim_id
        return self._change(add)

    def support(self, claim_id: str, reference: dict) -> None:
        def accept(book):
            claim = active_claim(book, claim_id)
            if self.review is None or not self.review.verifies(deepcopy(claim), reference):
                raise InvalidEvidence("no matching independent claim review")
            claim.update(state="supported", review=deepcopy(reference))
        self._change(accept)

    def relate(self, source: str, target: str, kind: str, reason: str) -> str:
        nonempty(reason, 10_000)
        relation_id = "R" + fingerprint([source, target, kind])
        def add(book):
            relation_allowed(book, source, target, kind)
            book["relations"][relation_id] = {"id": relation_id, "source": source, "target": target,
                                                "kind": kind, "reason": reason, "state": "active"}
            return relation_id
        return self._change(add)

    def bundle(self, claim_ids: list[str]) -> dict:
        """Include connected contradictions and correction history, never silently replace."""
        book = self.store.read()
        if not claim_ids or len(claim_ids) > 100:
            raise InvalidEvidence("expected 1..100 claims")
        selected = set(claim_ids)
        # Keep the complete related component, bounded by the output size below.
        while True:
            linked = {endpoint for r in book["relations"].values()
                      if r["state"] == "active" and (r["source"] in selected or r["target"] in selected)
                      for endpoint in (r["source"], r["target"])}
            if linked <= selected:
                break
            selected |= linked
            if len(selected) > 100:
                raise InvalidEvidence("evidence component exceeds context bound")
        claims = [active_claim(book, cid) for cid in sorted(selected)]
        source_ids = {sid for claim in claims for sid in claim["evidence"]}
        sources = {sid: deepcopy(active_source(book, sid)) for sid in sorted(source_ids)}
        relations = [deepcopy(r) for r in book["relations"].values()
                     if r["state"] == "active" and r["source"] in selected and r["target"] in selected]
        bundle = {"claims": deepcopy(claims), "sources": sources, "relations": relations}
        if len(str(bundle).encode()) > 400_000:
            raise InvalidEvidence("evidence bundle exceeds context bound")
        return {**bundle, "hash": fingerprint(bundle)}

    def register_consumer(self, source_ids: list[str], consumer: str) -> None:
        nonempty(consumer, 300)
        def register(book):
            for sid in source_ids:
                active_source(book, sid)
            book["consumers"][consumer] = sorted(set(source_ids))
        self._change(register)

    def retract(self, source_id: str) -> None:
        def mark(book):
            source = active_source(book, source_id)
            source["state"] = "retracted"
            for claim in book["claims"].values():
                if source_id in claim["evidence"]:
                    claim["state"] = "invalidated"
        self._change(mark)

    def erase(self, source_id: str) -> dict:
        """Erase this store's derived text; report every known external consumer separately."""
        def remove(book):
            source = book["sources"].get(source_id)
            if source is None:
                raise InvalidEvidence("unknown source")
            if source["state"] == "erased":
                return book["erasures"][source_id]
            affected = {cid for cid, c in book["claims"].items() if source_id in c["evidence"]}
            book["sources"][source_id] = {"id": source_id, "state": "erased", "content_hash": source["content_hash"]}
            for cid in affected:
                old = book["claims"][cid]
                book["claims"][cid] = {"id": cid, "state": "erased", "hash": old["hash"],
                                       "evidence": old["evidence"], "review": None}
            for relation in book["relations"].values():
                if {relation["source"], relation["target"]} & affected:
                    relation.update(state="erased", reason=None)
            consumers = [name for name, ids in book["consumers"].items() if source_id in ids]
            receipt = {"source_id": source_id, "local_state": "erased", "claims_erased": sorted(affected),
                       "consumers_requiring_erasure": sorted(consumers),
                       "scope": "current local library; backups, exports, jobs and provider history are separate"}
            book["erasures"][source_id] = receipt
            return receipt
        return self._change(remove)

    def status(self) -> dict:
        return self.store.read()

    def evidence_valid(self, references: dict[str, str]) -> bool:
        book = self.store.read()
        try:
            return bool(references) and all(active_source(book, sid)["content_hash"] == expected
                                            for sid, expected in references.items())
        except InvalidEvidence:
            return False
