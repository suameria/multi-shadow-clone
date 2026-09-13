from copy import deepcopy
import unittest

from multi_shadow_clone.orchestration.domain.agent_settings import AgentSettings
from multi_shadow_clone.orchestration.domain.contracts import InvalidContract


class AgentSettingsTest(unittest.TestCase):
    def setUp(self):
        self.catalog = {"gpt-5.6-luna": ("low", "medium"), "gpt-6-astra": ("low", "high", "ultra")}
        self.value = {"default": {"model": "gpt-5.6-luna", "effort": "low"},
                      "overrides": {"R12": {"model": "gpt-6-astra", "effort": "high"}}}

    def test_immutable_choices_include_independent_auditor(self):
        settings = AgentSettings.parse(self.value, {"R01", "R12"}, self.catalog)
        original = settings.fingerprint()
        self.value["overrides"]["R12"]["effort"] = "low"
        self.assertEqual(settings.for_role("R01").model, "gpt-5.6-luna")
        self.assertEqual(settings.for_role("R12").effort, "high")
        settings.record()["overrides"]["R12"]["effort"] = "low"
        self.assertEqual(settings.fingerprint(), original)
        self.assertNotEqual(AgentSettings.parse(self.value, {"R01", "R12"}, self.catalog).fingerprint(), original)

    def test_unavailable_or_unpermitted_choices_rejected(self):
        for model, effort in [("other-provider", "low"), ("gpt-5.6-luna", "high"), ("gpt-6-astra", "ultra")]:
            value = deepcopy(self.value)
            value["default"] = {"model": model, "effort": effort}
            with self.assertRaises(InvalidContract): AgentSettings.parse(value, {"R12"}, self.catalog)
        for field in ("service_tier", "api_key", "fallback"):
            value = deepcopy(self.value); value["default"][field] = "anything"
            with self.assertRaises(InvalidContract): AgentSettings.parse(value, {"R12"}, self.catalog)
        with self.assertRaises(InvalidContract): AgentSettings.parse(self.value, {"R01"}, self.catalog)
