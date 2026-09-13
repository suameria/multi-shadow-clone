"""Coordinate knowledge with the public job port, never with another database."""
from .library import Library


class KnowledgeWorkflow:
    def __init__(self, library: Library, jobs):
        self.library, self.jobs = library, jobs

    def handoff(self, objective: str, claim_ids: list[str]) -> dict:
        bundle = self.library.bundle(claim_ids)
        run_id = self.jobs.create(objective, bundle)
        # Job stores its references in the same transaction as its first text copy.
        # The job port scans authoritative references if registration is interrupted.
        self.library.register_consumer(list(bundle["sources"]), "job:" + run_id)
        return {"run_id": run_id, "state": "saved_not_started", "bundle_hash": bundle["hash"]}

    def erase(self, source_id: str) -> dict:
        local = self.library.erase(source_id)
        jobs = self.jobs.erase(source_id)
        return {"library": local, "consumers": jobs, "all_copies_erased": False,
                "remaining_scope": "provider history, exported files, backups and posted copies require their owners"}

    def prepare_review(self, claim_id: str) -> dict:
        bundle = self.library.bundle([claim_id])
        run_id = self.jobs.review(claim_id, bundle)
        self.library.register_consumer(list(bundle["sources"]), "job:" + run_id)
        return {"run_id": run_id, "node_id": "claimReview", "claim_id": claim_id,
                "state": "saved_not_started", "bundle_hash": bundle["hash"]}
