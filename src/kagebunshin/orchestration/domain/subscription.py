"""Fail-closed normalization for an included-usage-only execution policy."""

from decimal import Decimal, InvalidOperation

from .contracts import InvalidContract


def included_usage(account: dict, rates: dict, now: float, *, model: str) -> dict:
    """Return a non-identifying receipt, never credentials or billing actions."""
    if not isinstance(account, dict) or account.get("type") != "chatgpt" or account.get("planType") != "pro":
        raise InvalidContract("expected existing ChatGPT Pro subscription")
    if rates.get("ordinaryUsageAllowed") is not True:
        raise InvalidContract("ordinary included usage is not confirmed")
    if model not in {"gpt-6-astra", "gpt-5.6-luna"}:
        raise InvalidContract("model-to-bucket mapping has not been accepted for this model")
    buckets = rates.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets or "codex" not in buckets:
        raise InvalidContract("missing applicable Codex bucket")
    summary = {}
    excluded = []
    for key, bucket in buckets.items():
        # The observed separate Spark pool is not a fallback for the accepted Astra/Luna
        # profile. Unknown pools are not silently excluded. Labels/IDs are bound.
        if key == "codex_bengalfox" and isinstance(bucket, dict) and bucket.get("limitId") == key and bucket.get("limitName") == "GPT-5.3-Codex-Spark":
            excluded.append(key)
            continue
        if not isinstance(bucket, dict) or key != "codex" or bucket.get("limitId") != key:
            raise InvalidContract("unmapped quota bucket")
        if not isinstance(bucket, dict) or bucket.get("planType") != "pro" or bucket.get("spendControlReached") is not False:
            raise InvalidContract("unknown or conflicting spend-control state")
        credits = bucket.get("credits")
        if not isinstance(credits, dict) or credits.get("hasCredits") is not False or credits.get("unlimited") is not False:
            raise InvalidContract("credits must be known and unused")
        try:
            balance = Decimal(credits["balance"]) if isinstance(credits.get("balance"), str) else None
        except InvalidOperation:
            balance = None
        if balance is None or not balance.is_finite() or balance != 0:
            raise InvalidContract("credits balance must be explicitly zero")
        windows = []
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if name == "secondary" and window is None:
                continue
            if not isinstance(window, dict):
                raise InvalidContract("missing quota window")
            used, duration, reset = (window.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt"))
            if (type(used) is not int or not 0 <= used < 100 or type(duration) is not int or duration <= 0
                or type(reset) is not int or not now < reset <= now + duration * 60 + 300):
                raise InvalidContract("invalid, stale or exhausted quota")
            windows.append({"name": name, "used_percent": used, "reset_at": reset})
        summary[key] = windows
    return {"account_type": "chatgpt", "plan": "pro", "ordinary_usage_allowed": True,
            "credits_balance": "0", "checked_at": now, "buckets": summary, "model": model,
            "excluded_non_applicable_buckets": excluded,
            "scope": "account snapshot, not atomic account-wide reservation or purchase-settings audit"}
