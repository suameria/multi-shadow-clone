"""Evidence graph rules. A newer timestamp is never proof of correctness."""

from hashlib import sha256
import json


class InvalidEvidence(ValueError):
    pass


def fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode()).hexdigest()


def nonempty(value: object, bound: int = 100_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > bound:
        raise InvalidEvidence("expected bounded nonempty text")
    return value


def active_source(book: dict, source_id: str) -> dict:
    source = book["sources"].get(source_id)
    if not source or source["state"] != "active":
        raise InvalidEvidence("source is missing, retracted or erased")
    if fingerprint(source["content"]) != source["content_hash"]:
        raise InvalidEvidence("source snapshot changed")
    return source


def active_claim(book: dict, claim_id: str) -> dict:
    claim = book["claims"].get(claim_id)
    if not claim or claim["state"] not in {"candidate", "supported"}:
        raise InvalidEvidence("claim is unavailable")
    if fingerprint({"statement": claim["statement"], "evidence": claim["evidence"]}) != claim["hash"]:
        raise InvalidEvidence("claim changed")
    for source_id, expected in claim["evidence"].items():
        if active_source(book, source_id)["content_hash"] != expected:
            raise InvalidEvidence("claim evidence changed")
    return claim


def relation_allowed(book: dict, source: str, target: str, kind: str) -> None:
    a, b = active_claim(book, source), active_claim(book, target)
    if source == target or kind not in {"supports", "contradicts", "supersedes"}:
        raise InvalidEvidence("invalid relation")
    if kind == "supersedes" and a["state"] != "supported":
        raise InvalidEvidence("a correction needs a bound independent review")
    if kind != "contradicts":
        edges = [(r["source"], r["target"]) for r in book["relations"].values()
                 if r["state"] == "active" and r["kind"] != "contradicts"]
        todo, seen = [target], set()
        while todo:
            node = todo.pop()
            if node == source:
                raise InvalidEvidence("directed evidence relation cycle")
            if node not in seen:
                seen.add(node)
                todo.extend(b for a, b in edges if a == node)
