from typing import Protocol


class Conflict(RuntimeError):
    pass


class KnowledgeStore(Protocol):
    def read(self) -> dict: ...
    def save(self, book: dict, expected_revision: int) -> None: ...


class ClaimReview(Protocol):
    def verifies(self, claim: dict, reference: dict) -> bool:
        """Check an independently audited, immutable result against this exact claim."""
        ...


class EvidenceJobs(Protocol):
    def create(self, objective: str, bundle: dict) -> str: ...
    def review(self, claim_id: str, bundle: dict) -> str: ...
    def erase(self, source_id: str) -> dict: ...
