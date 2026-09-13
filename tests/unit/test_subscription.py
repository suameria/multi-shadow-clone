from copy import deepcopy
import unittest

from kagebunshin.orchestration.domain.contracts import InvalidContract
from kagebunshin.orchestration.domain.subscription import included_usage


class SubscriptionTest(unittest.TestCase):
    def setUp(self):
        self.account = {"type": "chatgpt", "planType": "pro"}
        self.rates = {"ordinaryUsageAllowed": True, "rateLimitsByLimitId": {"codex": {
            "limitId": "codex", "planType": "pro", "spendControlReached": False,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 2000}, "secondary": None}}}

    def validate(self):
        return included_usage(self.account, self.rates, 1000, model="gpt-6-astra")

    def test_luna_preserves_included_only_guards(self):
        result = included_usage(self.account, self.rates, 1000, model="gpt-5.6-luna")
        self.assertEqual(result["model"], "gpt-5.6-luna")
        self.rates["ordinaryUsageAllowed"] = False
        with self.assertRaises(InvalidContract):
            included_usage(self.account, self.rates, 1000, model="gpt-5.6-luna")
        self.rates["ordinaryUsageAllowed"] = True
        self.rates["rateLimitsByLimitId"]["codex"]["credits"]["hasCredits"] = True
        with self.assertRaises(InvalidContract):
            included_usage(self.account, self.rates, 1000, model="gpt-5.6-luna")

    def test_reject_api_or_unknown_auth(self):
        for account in [{"type": "apiKey"}, {}, {"type": "chatgpt", "planType": "unknown"}]:
            self.account = account
            with self.assertRaises(InvalidContract):
                self.validate()

    def test_ordinary_usage_must_be_explicitly_allowed(self):
        for value in [False, None, 1, "true"]:
            self.rates["ordinaryUsageAllowed"] = value
            with self.assertRaises(InvalidContract):
                self.validate()

    def test_invalid_quota_and_reset_are_rejected(self):
        for field, value in [("usedPercent", True), ("usedPercent", float("nan")), ("usedPercent", 100),
                             ("usedPercent", -1), ("resetsAt", 500), ("windowDurationMins", None)]:
            saved = deepcopy(self.rates)
            self.rates["rateLimitsByLimitId"]["codex"]["primary"][field] = value
            with self.assertRaises(InvalidContract):
                self.validate()
            self.rates = saved

    def test_unknown_or_available_credits_are_rejected(self):
        for credits in [None, {"hasCredits": False, "unlimited": False, "balance": None},
                        {"hasCredits": True, "unlimited": False, "balance": "1"},
                        {"hasCredits": False, "unlimited": False, "balance": "NaN"}]:
            self.rates["rateLimitsByLimitId"]["codex"]["credits"] = credits
            with self.assertRaises(InvalidContract):
                self.validate()

    def test_only_identified_non_applicable_spark_bucket_is_excluded(self):
        self.rates["rateLimitsByLimitId"]["codex_bengalfox"] = {"limitId": "codex_bengalfox", "limitName": "GPT-5.3-Codex-Spark", "credits": None}
        result = self.validate()
        self.assertEqual(result["excluded_non_applicable_buckets"], ["codex_bengalfox"])
        self.rates["rateLimitsByLimitId"]["codex_bengalfox"]["limitName"] = "unknown model"
        with self.assertRaises(InvalidContract):
            self.validate()

    def test_unknown_model_mapping_never_uses_default_pool(self):
        with self.assertRaises(InvalidContract):
            included_usage(self.account, self.rates, 1000, model="unmapped")

    def test_remaining_included_quota_is_allowed_but_not_paid_fallback(self):
        for used in (90, 91, 99):
            self.rates['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = used
            self.assertTrue(self.validate()['ordinary_usage_allowed'])
        self.rates['ordinaryUsageAllowed'] = False
        with self.assertRaises(InvalidContract):
            self.validate()
