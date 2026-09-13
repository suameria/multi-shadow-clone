"""Translate a knowledge bundle to the orchestration public creation contract."""
class EvidenceJobs:
    def __init__(self, engine):
        self.engine = engine

    def create(self, objective, bundle):
        return self.engine.submit_evidence(objective, bundle)

    def erase(self, source_id):
        return self.engine.erase_evidence(source_id)

    def review(self, claim_id, bundle):
        return self.engine.submit_claim_review(claim_id, bundle)
