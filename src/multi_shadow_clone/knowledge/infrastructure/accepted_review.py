"""Bridge to the orchestration public result boundary, not its database."""


class AcceptedClaimReview:
    def __init__(self, results):
        self.results = results

    def verifies(self, claim: dict, reference: dict) -> bool:
        if set(reference) != {"run_id", "node_id"}:
            return False
        try:
            result = self.results.accepted_result(reference["run_id"], reference["node_id"])
        except (KeyError, ValueError):
            return False
        expected = {"claim_id": claim["id"], "claim_hash": claim["hash"],
                    "source_hashes": claim["evidence"], "supported": True}
        return (result.get("provider_contract", {}).get("kind") == "codex-subscription"
                and result["audit_role"] is not None and result["audit_role"] != result["role_id"]
                and result["receipt"].get("audit_hash") is not None
                and result["output"]["values"].get("claim_review") == expected)
