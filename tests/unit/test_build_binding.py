import unittest

from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.domain.contracts import InvalidContract, Node, Plan
from multi_shadow_clone.orchestration.ports import Result
from tests.unit.fakes import MemoryStore, ROLES, ScriptedProvider, candidate


class Build:
    version = "first-code"
    def current(self): return {"code": self.version}


class BuildBindingTest(unittest.TestCase):
    def test_checker_code_change_invalidates_accepted_result_without_new_model_turn(self):
        build, provider = Build(), ScriptedProvider()
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000, build_identity=build)
        job = engine.create(Plan("fixture", (Node("a", "R07", "answer", audit_role="R12"),), {}))
        engine.run_until_idle(job)
        engine.accepted_result(job, "a")
        turns = len(provider.requests)
        build.version = "second-code-same-schema"
        with self.assertRaises(InvalidContract): engine.accepted_result(job, "a")
        engine.resume(job); engine.run_until_idle(job)
        self.assertEqual(engine.status(job)["state"], "blocked_contract_changed")
        self.assertEqual(len(provider.requests), turns)

    def test_update_during_model_execution_quarantines_result(self):
        build = Build()
        def handle(request):
            build.version = "updated-executable"
            self.assertFalse(request.may_continue())
            return Result("completed", candidate())
        engine = Engine(MemoryStore(), ScriptedProvider(handle), ROLES, lambda: 1000, build_identity=build)
        job = engine.create(Plan("fixture", (Node("a", "R07", "answer"),), {}))
        engine.step(job)
        self.assertEqual(engine.status(job)["nodes"]["a"]["state"], "quarantined")

    def test_legacy_record_without_build_binding_is_history_only(self):
        store, provider = MemoryStore(), ScriptedProvider()
        engine = Engine(store, provider, ROLES, lambda: 1000)
        job = engine.create(Plan("fixture", (Node("a", "R07", "answer"),), {}))
        del store.data[job]["build_contract"]
        del store.data[job]["build_hash"]
        engine.run_until_idle(job)
        self.assertEqual(engine.status(job)["state"], "blocked_contract_changed")
        self.assertEqual(provider.requests, [])
