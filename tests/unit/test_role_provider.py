import unittest
from kagebunshin.orchestration.application.role_provider import RoleProvider
from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.domain.agent_settings import AgentSettings, ModelChoice
from kagebunshin.orchestration.domain.contracts import Node, Plan
from kagebunshin.orchestration.ports import ProviderBlocked
from tests.unit.fakes import MemoryStore, ScriptedProvider, ROLES


class RoleProviderTest(unittest.TestCase):
    def test_generation_and_audit_use_frozen_distinct_providers(self):
        settings = AgentSettings(overrides=(("R12", ModelChoice("gpt-6-astra", "low")),))
        generation, audit = ScriptedProvider(), ScriptedProvider()
        generation.contract = lambda: settings.for_role("R07").record()
        audit.contract = lambda: settings.for_role("R12").record()
        routed = RoleProvider(settings, {"R07": generation, "R12": audit})
        engine = Engine(MemoryStore(), routed, ROLES, lambda: 1000)
        frozen = {"revision": 1, "settings": settings.record(), "settings_hash": settings.fingerprint()}
        run_id = engine.create(Plan("fixture", (Node("a", "R07", "read", audit_role="R12"),), {}), execution_settings=frozen)
        frozen["settings"]["default"]["model"] = "edited-after-save"
        self.assertEqual(engine.status(run_id)["execution_settings"]["settings"]["default"]["model"], "gpt-5.6-luna")
        self.assertEqual(engine.run_until_idle(run_id)["state"], "completed")
        self.assertEqual([r.role_id for r in generation.requests], ["R07"])
        self.assertEqual([r.role_id for r in audit.requests], ["R12"])
        self.assertEqual((generation.preflights, audit.preflights), (1, 1))
        audit.reconciled = "same auditor result"
        self.assertEqual(routed.reconcile({"role_id": "R12"}), "same auditor result")
        with self.assertRaises(ProviderBlocked): routed.preflight_for("unknown")

    def test_wrong_provider_cannot_claim_selected_model(self):
        provider = ScriptedProvider()
        provider.contract = lambda: {"model": "other", "effort": "low", "service_tier": "default"}
        with self.assertRaises(ProviderBlocked): RoleProvider(AgentSettings(), {"R07": provider})

    def test_lifecycle_uses_owner_and_closes_shared_providers_once(self):
        settings = AgentSettings()
        first, second = ScriptedProvider(), ScriptedProvider()
        for provider in (first, second):
            provider.contract = lambda: settings.default.record()
        archived, closed = [], []
        first.archive_owned = lambda thread, turn: archived.append(("first", thread, turn))
        second.archive_owned = lambda thread, turn: archived.append(("second", thread, turn))
        def fail_close():
            closed.append("first")
            raise OSError("fixture")
        first.close = fail_close
        second.close = lambda: closed.append("second")
        first.usage_snapshot = lambda: {"observed_at": 10, "used_percent": 20}
        second.usage_snapshot = lambda: {"observed_at": 20, "used_percent": 25}
        routed = RoleProvider(settings, {"R01": first, "R07": first, "R12": second})
        routed.archive_for("R12", "owned-thread", "owned-turn")
        self.assertEqual(archived, [("second", "owned-thread", "owned-turn")])
        self.assertEqual(routed.usage_snapshot()["used_percent"], 25)
        with self.assertRaises(RuntimeError): routed.close()
        self.assertEqual(closed, ["first", "second"])
