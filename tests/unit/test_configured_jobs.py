import unittest
from multi_shadow_clone.orchestration.application.configured_jobs import ConfiguredJobs
from multi_shadow_clone.orchestration.application.settings import Settings
from multi_shadow_clone.orchestration.application.overview import Overview
from multi_shadow_clone.orchestration.application.operator import Operator
from multi_shadow_clone.orchestration.application.role_provider import RoleProvider
from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.domain.contracts import Limits
from tests.unit.fakes import MemoryStore, ScriptedProvider, ROLES


class ConfiguredJobsTest(unittest.TestCase):
    def test_saved_job_keeps_old_choice_after_default_changes(self):
        class Store:
            revision = 1
            def read(self, revision=None):
                return {"revision": self.revision, "value": {"default": {"model": "gpt-5.6-luna", "effort": "low" if self.revision == 1 else "medium"}, "overrides": {}}}
        store = Store()
        settings = Settings(store, ROLES, {"gpt-5.6-luna": ("low", "medium")})
        db = MemoryStore()
        base = Engine(db, ScriptedProvider(), ROLES, lambda: 1000)
        def factory(choice):
            provider = ScriptedProvider()
            provider.contract = lambda: choice.default.record()
            return Engine(db, RoleProvider(choice, {r: provider for r in ROLES}), ROLES, lambda: 1000)
        configured = ConfiguredJobs(base, settings, factory, lambda e: e.run_until_idle)
        first = configured.submit("first", {}, Limits())
        store.revision = 2
        second = configured.submit("second", {}, Limits())
        self.assertEqual(configured.for_run(first).provider.settings.default.effort, "low")
        self.assertEqual(configured.for_run(second).provider.settings.default.effort, "medium")
        self.assertTrue(configured.for_run(first).execution_status(first)["compatible"])
        self.assertEqual(base.status(first)["execution_settings"]["revision"], 1)

        # A removed effort must not hide unrelated jobs or prevent STOP.
        settings.catalog = {"gpt-5.6-luna": ("medium",)}
        view = Overview(base, lambda: 1000, configured.for_run).snapshot()
        jobs = {j["id"]: j for j in view["jobs"]}
        self.assertFalse(jobs[first]["execution_available"])
        self.assertEqual(jobs[first]["acceptance"]["reason"], "settings_unavailable")
        self.assertEqual(jobs[first]["results"], [])
        self.assertFalse(jobs[first]["can_cleanup"])
        self.assertTrue(jobs[second]["acceptance"]["compatible"])
        operator = Operator(base, None, None, lambda: 1000, configured_jobs=configured)
        before = base.status(first)["stop_epoch"]
        self.assertEqual(operator.act("stop", first)["state"], "stopped")
        self.assertEqual(base.status(first)["stop_epoch"], before + 1)
        self.assertEqual(base.status(first)["turns"], 0)
