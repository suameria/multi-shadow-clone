"""Deterministic candidate checks and receipt binding."""

from dataclasses import asdict, dataclass
import math
from typing import Any

from .contracts import Node, canonical, digest


@dataclass(frozen=True)
class Defect:
    code: str
    target_path: str
    expected: Any
    observed: Any
    evidence: str
    repairable: bool = True


CHECKER_VERSION = "response-checkers-v2"


def check(node: Node, payload: Any) -> list[dict]:
    defects: list[Defect] = []
    if not isinstance(payload, dict) or set(payload) != {"text", "source_ids", "limits", "values"}:
        return [asdict(Defect("schema", "$", "response.v1 fields", type(payload).__name__, "envelope"))]
    if (not isinstance(payload["text"], str) or not payload["text"].strip()
        or not isinstance(payload["limits"], list) or any(not isinstance(x, str) for x in payload["limits"])
        or not isinstance(payload["values"], dict)
        or not isinstance(payload["source_ids"], list) or any(not isinstance(x, str) for x in payload["source_ids"])):
        return [asdict(Defect("schema", "$", "typed response.v1", "invalid fields", "envelope"))]
    try:
        if len(canonical(payload).encode()) > 100_000:
            raise ValueError("too large")
    except (ValueError, TypeError):
        return [asdict(Defect("serialization", "$", "finite bounded JSON", "invalid", "serialization"))]
    from .output_format import values_match
    if node.response_fields and not values_match(node.response_fields, payload["values"]):
        defects.append(Defect("value_types", "values", node.response_fields, payload["values"], "declared response fields"))
    if not set(payload["source_ids"]) <= set(node.source_ids):
        defects.append(Defect("source_unknown", "source_ids", list(node.source_ids), payload["source_ids"], "registered sources"))
    if set(node.source_ids) - set(payload["source_ids"]):
        defects.append(Defect("source_missing", "source_ids", list(node.source_ids), payload["source_ids"], "required evidence"))
    for rule in node.rules:
        observed: Any = payload
        missing = False
        for part in rule.path.split("."):
            if not isinstance(observed, dict) or part not in observed:
                missing = True
                break
            observed = observed[part]
        if missing:
            defects.append(Defect("path_missing", rule.path, "present field", "missing", CHECKER_VERSION))
            continue
        if rule.kind == "equals":
            if type(rule.expected) in {int, float}:
                passed = type(observed) in {int, float} and math.isfinite(observed) and observed == rule.expected
            else:
                passed = type(observed) is type(rule.expected) and observed == rule.expected
        elif rule.kind == "contains":
            passed = isinstance(observed, (str, list)) and isinstance(rule.expected, str) and rule.expected in observed
        else:
            numeric = type(observed) in {int, float} and math.isfinite(observed)
            passed = numeric and (observed >= rule.expected if rule.kind == "minimum" else observed <= rule.expected)
        if not passed:
            defects.append(Defect("rule_" + rule.kind, rule.path, rule.expected, observed, CHECKER_VERSION))
    return [asdict(d) for d in defects]


def audit_defects(payload: Any) -> list[dict]:
    """An empty or contradictory audit cannot approve a candidate."""
    bad = [asdict(Defect("audit_invalid", "$", "approved/reason/defects", "malformed audit", "auditor contract", False))]
    if not isinstance(payload, dict) or set(payload) != {"approved", "reason", "defects"}:
        return bad
    if type(payload["approved"]) is not bool or not isinstance(payload["reason"], str) or not payload["reason"].strip():
        return bad
    if not isinstance(payload["defects"], list) or payload["approved"] != (len(payload["defects"]) == 0):
        return bad
    for item in payload["defects"]:
        if not isinstance(item, dict) or set(item) != set(Defect.__dataclass_fields__):
            return bad
        if (any(not isinstance(item[k], str) for k in ("code", "target_path", "evidence"))
            or type(item["repairable"]) is not bool):
            return bad
    return payload["defects"]


def receipt(binding: str, candidate: dict, audit: dict | None) -> dict:
    return {"binding_hash": binding, "artifact_hash": digest(candidate),
            "checker": CHECKER_VERSION, "audit_hash": digest(audit) if audit is not None else None}
