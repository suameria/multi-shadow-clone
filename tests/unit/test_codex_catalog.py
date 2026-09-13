"""Pure transformation of the bundled model's tool advertisement."""
from copy import deepcopy
import unittest

from multi_shadow_clone.orchestration.infrastructure.codex_catalog import restricted_catalog, TOOL_FIELDS
from multi_shadow_clone.orchestration.ports import ProviderBlocked


def fixture():
    return {"models": [{"slug": "gpt-6-astra", "tool_mode": "code_mode_only",
                        "experimental_supported_tools": ["clock", "send_user_message_async"],
                        "shell_type": "unified_exec", "apply_patch_tool_type": "freeform",
                        "supported_reasoning_levels": [{"effort": "max"}],
                        "use_responses_lite": True, "context_window": 123,
                        "base_instructions": "model-specific instructions"},
                       {"slug": "other-model"}]}


class CatalogTransformationTest(unittest.TestCase):
    def test_only_tool_advertisements_change_and_no_fallback_model_survives(self):
        original = fixture()
        before = deepcopy(original)
        result = restricted_catalog(original, "gpt-6-astra")
        self.assertEqual(original, before)
        self.assertEqual(len(result["models"]), 1)
        selected = result["models"][0]
        self.assertEqual({k: v for k, v in selected.items() if k not in TOOL_FIELDS},
                         {k: v for k, v in original["models"][0].items() if k not in TOOL_FIELDS})
        self.assertEqual({k: selected[k] for k in TOOL_FIELDS}, TOOL_FIELDS)

    def test_unknown_metadata_and_ambiguous_model_cannot_silently_fall_back(self):
        invalid = [None, {"models": []}, {"models": ["bad"]}, {"models": [fixture()["models"][0]] * 2}]
        for key, value in [("tool_mode", "unknown"), ("experimental_supported_tools", None), ("shell_type", "unknown")]:
            changed = fixture()
            changed["models"][0][key] = value
            invalid.append(changed)
        for original in invalid:
            with self.subTest(original=original), self.assertRaises(ProviderBlocked):
                restricted_catalog(original, "gpt-6-astra")
