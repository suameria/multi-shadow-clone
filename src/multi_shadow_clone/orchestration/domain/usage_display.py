"""Read-only weekly display projection; never a dispatch or billing decision.

Selection follows Suameria CodexRateLimitsParser: codex bucket, then legacy,
primary or secondary identified by duration rather than position.
"""
from math import isfinite


def weekly_usage(rates: dict, observed_at: float) -> dict:
    result = {"scope": "account", "observed_at": observed_at, "available": False}
    if not isinstance(rates, dict):
        return result
    buckets = rates.get("rateLimitsByLimitId")
    preferred = buckets.get("codex") if isinstance(buckets, dict) else None
    for bucket in (preferred, rates.get("rateLimits")):
        if not isinstance(bucket, dict):
            continue
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if not isinstance(window, dict) or type(window.get("windowDurationMins")) is not int or window["windowDurationMins"] != 10080:
                continue
            used = window.get("usedPercent")
            if type(used) not in (int, float) or not isfinite(used) or not 0 <= used <= 100:
                return result
            reset = window.get("resetsAt")
            if type(reset) not in (int, float) or not isfinite(reset) or reset <= 0:
                reset = None
            return {**result, "available": True, "used_percent": used,
                    "remaining_percent": 100 - used, "reset_at": reset,
                    "window_duration_minutes": 10080}
    return result
