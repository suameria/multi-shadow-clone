import unittest
from kagebunshin.orchestration.domain.usage_display import weekly_usage


class UsageDisplayTests(unittest.TestCase):
    def test_new_profile_defaults_to_requested_luna_low(self):
        from pathlib import Path
        from kagebunshin.orchestration.infrastructure.codex_provider import CodexProfile
        profile = CodexProfile(Path("/fake/codex"), Path("/fake/workspace"))
        self.assertEqual((profile.model, profile.effort, profile.service_tier), ("gpt-5.6-luna", "low", "default"))

    def test_weekly_selection_and_account_scope(self):
        weekly = {"windowDurationMins": 10080, "usedPercent": 31.5, "resetsAt": 1000}
        rates = {"rateLimitsByLimitId": {"codex": {"primary": weekly}},
                 "rateLimits": {"secondary": {**weekly, "usedPercent": 99}}}
        result = weekly_usage(rates, 100)
        self.assertEqual(result["remaining_percent"], 68.5)
        self.assertEqual(result["scope"], "account")
        self.assertEqual(weekly_usage({"rateLimits": {"secondary": weekly}}, 100), result)

    def test_invalid_is_unavailable_not_zero(self):
        for used in (True, float("nan"), float("inf"), -1, 101, "20", None):
            rates = {"rateLimits": {"secondary": {"windowDurationMins": 10080, "usedPercent": used}}}
            self.assertFalse(weekly_usage(rates, 100)["available"])
        self.assertFalse(weekly_usage({}, 100)["available"])
        result = weekly_usage({"rateLimits": {"primary": {"windowDurationMins": 10080, "usedPercent": 0, "resetsAt": True}}}, 100)
        self.assertTrue(result["available"])
        self.assertIsNone(result["reset_at"])
