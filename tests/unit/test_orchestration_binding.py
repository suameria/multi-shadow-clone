import unittest

from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.domain.contracts import InvalidContract, Node, Plan
from kagebunshin.orchestration.ports import Result
from tests.unit.fakes import MemoryStore, ROLES, ScriptedProvider, candidate


class BindingTest(unittest.TestCase):
    def test_same_provider_thread_cannot_audit_its_own_generation(self):
        def handler(request):
            payload = {"approved": True, "reason": "self audit", "defects": []} if request.stage == "audit" else candidate()
            return Result("completed", payload, "reused-thread", request.stage)
        engine = Engine(MemoryStore(), ScriptedProvider(handler), ROLES, lambda: 1000)
        run_id = engine.create(Plan("task", (Node("a", "R07", "a", audit_role="R12"),), {}))
        result = engine.run_until_idle(run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "blocked_validation")
        with self.assertRaises(InvalidContract):
            engine.accepted_result(run_id, "a")

    def test_public_result_rejects_a_changed_independent_audit(self):
        store = MemoryStore()
        engine = Engine(store, ScriptedProvider(), ROLES, lambda: 1000)
        run_id = engine.create(Plan("task", (Node("a", "R07", "a", audit_role="R12"),), {}))
        engine.run_until_idle(run_id)
        self.assertTrue(engine.accepted_result(run_id, "a")["audit"]["approved"])
        run = store.read(run_id)
        run["nodes"]["a"]["audit"]["reason"] = "changed audit"
        store.save(run, run["revision"])
        with self.assertRaises(InvalidContract):
            engine.accepted_result(run_id, "a")

    def test_changed_accepted_output_never_reaches_downstream(self):
        store, provider = MemoryStore(), ScriptedProvider()
        engine = Engine(store, provider, ROLES, lambda: 1000)
        run_id = engine.create(Plan("task", (Node("a", "R07", "a"), Node("b", "R07", "b", ("a",), {"a": "response.v1"})), {}))
        engine.step(run_id)
        run = store.read(run_id)
        run["nodes"]["a"]["output"] = candidate(999)
        store.save(run, run["revision"])
        result = engine.run_until_idle(run_id)
        self.assertEqual(result["state"], "blocked_artifact_changed")
        self.assertEqual(len(provider.requests), 1)

    def test_approval_cannot_apply_to_candidate_swapped_during_audit(self):
        store = MemoryStore()
        def handler(request):
            if request.stage == "audit":
                run = store.read(request.run_id)
                run["nodes"]["a"]["candidate"] = candidate(999)
                store.save(run, run["revision"])
                return Result("completed", {"approved": True, "reason": "old candidate", "defects": []})
            return Result("completed", candidate())
        provider = ScriptedProvider(handler)
        engine = Engine(store, provider, ROLES, lambda: 1000)
        run_id = engine.create(Plan("task", (Node("a", "R07", "a", audit_role="R12"),), {}))
        result = engine.run_until_idle(run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "blocked_artifact_changed")
        self.assertIsNone(result["nodes"]["a"]["output"])
