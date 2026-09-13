"""Hidden-answer factual scoring. A passing sample never promotes an expert."""
from hashlib import sha256
import json
import math


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def candidate_pack(case):
    required = {"id", "input", "fields", "expected", "basis", "held_out"}
    if set(case) not in (required, required | {"role_id", "role_contract_sha256"}):
        raise ValueError("unsupported evaluation case fields")
    if not isinstance(case["input"], dict) or not case["input"] or not all(isinstance(v, str) for v in case["input"].values()):
        raise ValueError("candidate input must be text snapshots")
    if set(case["fields"]) != set(case["expected"]):
        raise ValueError("scoring and response fields disagree")
    schema = json.dumps(case["fields"], ensure_ascii=False)
    return {**case["input"], "responseFields": "Return the following fields inside response.v1 values. Field descriptions, not answers: " + schema}


def score(case, output):
    actual = output.get("values", {}) if isinstance(output, dict) else {}
    checks = []
    for key, expected in case["expected"].items():
        value = actual.get(key)
        if type(expected) in (int, float):
            ok = key in actual and type(value) in (int, float) and math.isfinite(value) and math.isclose(value, expected, rel_tol=1e-6, abs_tol=1e-6)
        else:
            ok = key in actual and type(value) is type(expected) and value == expected
        checks.append({"field": key, "passed": ok, "actual": value, "expected": expected})
    return {"factual_pass": bool(checks) and all(c["passed"] for c in checks), "checks": checks,
            "scope": "declared structured facts only; narrative quality and broad expertise are not scored"}
