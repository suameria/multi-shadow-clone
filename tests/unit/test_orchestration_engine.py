from dataclasses import replace
import json
import unittest

from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.domain.contracts import Limits, Node, Plan, Rule
from multi_shadow_clone.orchestration.ports import ProviderBlocked, ProviderUnknown, Result
from tests.unit.fakes import MemoryStore, ROLES, ScriptedProvider, candidate


class EngineTest(unittest.TestCase):
    def make(self, nodes=None, handler=None, limits=None):
        self.now = 1000
        self.store = MemoryStore()
        self.provider = ScriptedProvider(handler)
        self.engine = Engine(self.store, self.provider, ROLES, lambda: self.now)
        self.run_id = self.engine.create(Plan("synthetic task", tuple(nodes or [Node("a", "R07", "explain")]), {}, limits or Limits()))
        return self.engine

    def test_diamond_joins_only_accepted_dependencies(self):
        nodes = [Node("a", "R07", "a", audit_role="R12"), Node("b", "R07", "b", audit_role="R12"),
                 Node("c", "R07", "combine", ("a", "b"), {"a": "response.v1", "b": "response.v1"})]
        self.make(nodes)
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["turns"], 5)
        final_context = json.loads(self.provider.requests[-1].prompt.split("\nDATA_JSON\n")[1])
        self.assertEqual(set(final_context["contract"]["dependencies"]), {"a", "b"})
        acceptance = final_context["contract"]["dependency_acceptance"]
        for key in ("a", "b"):
            self.assertEqual(acceptance[key]["audit_role"], "R12")
            self.assertTrue(acceptance[key]["audit"]["approved"])
            self.assertEqual(acceptance[key]["receipt"], result["nodes"][key]["receipt"])
        events = result["events"]
        accepted = {e["node_id"]: e["seq"] for e in events if e["kind"] == "accepted"}
        c_reserved = next(e["seq"] for e in events if e["kind"] == "turn_reserved" and e["node_id"] == "c")
        self.assertLess(max(accepted["a"], accepted["b"]), c_reserved)

    def test_repairs_only_bad_sibling_with_specific_feedback(self):
        counts = {}
        def handler(request):
            counts[request.node_id] = counts.get(request.node_id, 0) + 1
            value = 999 if request.node_id == "b" and counts["b"] == 1 else 5
            return Result("completed", candidate(value))
        self.make([Node("a", "R19", "a"), Node("b", "R19", "mean", rules=(Rule("values.mean", "equals", 5),))], handler)
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(counts, {"a": 1, "b": 2})
        self.assertIn('"observed":999', self.provider.requests[-1].prompt)
        self.assertIn('"expected":5', self.provider.requests[-1].prompt)

    def test_repeated_same_error_stops_before_attempt_limit(self):
        self.make([Node("a", "R19", "mean", rules=(Rule("values.mean", "equals", 5),))], lambda _: Result("completed", candidate(999)))
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "no_progress")
        self.assertEqual(len(self.provider.requests), 2)

    def test_semantic_audit_and_generation_share_turn_budget(self):
        self.make([Node("a", "R07", "a", audit_role="R12")], limits=Limits(max_turns=1))
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["state"], "paused_budget")
        self.assertEqual(result["nodes"]["a"]["state"], "audit_pending")
        self.assertEqual(len(self.provider.requests), 1)

    def test_stop_resume_does_not_adopt_old_worker_result(self):
        def handler(request):
            self.engine.stop(request.run_id)
            self.engine.resume(request.run_id)
            return Result("completed", candidate())
        self.make(handler=handler)
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "quarantined")
        self.assertIsNone(result["nodes"]["a"]["output"])
        self.assertEqual(result["stop_epoch"], 2)

    def test_unknown_outcome_never_blindly_retries(self):
        def handler(_):
            raise ProviderUnknown()
        self.make(handler=handler)
        result = self.engine.run_until_idle(self.run_id)
        self.engine.reconcile(self.run_id)
        self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["nodes"]["a"]["state"], "unknown")
        self.assertEqual(len(self.provider.requests), 1)
        self.provider.reconciled = Result("completed", candidate())
        self.engine.reconcile(self.run_id)
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(len(self.provider.requests), 1)

    def test_duplicate_terminal_is_idempotent(self):
        self.make()
        result = self.engine.run_until_idle(self.run_id)
        self.engine._finish(self.run_id, result["attempts"][0]["id"], Result("completed", candidate(999)))
        self.assertEqual(self.engine.status(self.run_id), result)

    def test_restart_keeps_stop_and_does_not_extend_deadline(self):
        self.make(limits=Limits(deadline_seconds=5))
        self.engine.stop(self.run_id)
        new_engine = Engine(self.store, self.provider, ROLES, lambda: self.now)
        self.assertFalse(new_engine.step(self.run_id))
        self.now += 6
        new_engine.resume(self.run_id)
        result = new_engine.run_until_idle(self.run_id)
        self.assertEqual(result["state"], "paused_deadline")
        self.assertEqual(self.provider.requests, [])

    def test_changed_role_or_plan_blocks_before_dispatch(self):
        self.make()
        other = dict(ROLES)
        other["R07"] = replace(other["R07"], version=2)
        engine = Engine(self.store, self.provider, other, lambda: self.now)
        self.assertEqual(engine.run_until_idle(self.run_id)["state"], "blocked_contract_changed")
        self.assertEqual(self.provider.requests, [])

    def test_preflight_failure_sends_nothing(self):
        self.make()
        def denied():
            raise ProviderBlocked("account type")
        self.provider.preflight = denied
        self.assertEqual(self.engine.run_until_idle(self.run_id)["state"], "blocked_preflight")
        self.assertEqual(self.provider.requests, [])

    def test_failed_branch_does_not_block_independent_branch_and_its_dependent(self):
        def handler(request):
            return Result("failed") if request.node_id == "a" else Result("completed", candidate())
        self.make([Node("a", "R07", "failure"), Node("b", "R07", "independent"),
                   Node("c", "R07", "uses b", ("b",), {"b": "response.v1"})], handler)
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["nodes"]["a"]["state"], "blocked_provider")
        self.assertEqual([result["nodes"][n]["state"] for n in ("b", "c")], ["accepted", "accepted"])
        self.assertEqual([r.node_id for r in self.provider.requests], ["a", "b", "c"])
        self.assertIsNotNone(self.engine.accepted_result(self.run_id, "c")["receipt"])

    def test_cleanup_failure_retries_without_generation(self):
        self.make(handler=lambda _: Result('completed', candidate(), 'owned-thread', 'owned-turn'))
        calls = []
        def archive(thread, turn):
            calls.append((thread, turn))
            if len(calls) == 1:
                raise RuntimeError('temporary archive failure')
        self.provider.archive_owned = archive
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result['task_cleanup']['owned-thread']['state'], 'cleanup_pending')
        turns = result['turns']
        result = self.engine.cleanup(self.run_id)
        self.assertEqual(result['task_cleanup']['owned-thread']['state'], 'archived')
        self.engine.cleanup(self.run_id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['turns'], turns)

    def test_unknown_attempt_is_not_archived(self):
        self.make(handler=lambda _: Result('unknown', thread_id='owned-thread', turn_id='owned-turn'))
        calls = []
        self.provider.archive_owned = lambda *args: calls.append(args)
        self.engine.run_until_idle(self.run_id)
        self.engine.cleanup(self.run_id)
        self.assertEqual(calls, [])

    def test_unsent_owned_thread_is_cleaned_without_generation_retry(self):
        self.make(handler=lambda _: Result('not_sent', thread_id='owned-empty'))
        calls = []
        self.provider.archive_owned = lambda *args: calls.append(args)
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(calls, [('owned-empty', None)])
        self.assertEqual(result['task_cleanup']['owned-empty']['state'], 'archived')
        self.engine.cleanup(self.run_id)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(len(calls), 1)

    def test_provider_unsent_release_is_saved_without_archive_retry(self):
        self.make(handler=lambda _: Result('not_sent', thread_id='owned-empty', cleanup_state='released_unmaterialized'))
        calls = []
        self.provider.archive_owned = lambda *args: calls.append(args)
        result = self.engine.run_until_idle(self.run_id)
        self.assertEqual(result['task_cleanup']['owned-empty']['state'], 'released_unmaterialized')
        self.engine.cleanup(self.run_id)
        self.assertEqual(calls, [])

    def test_model_payload_cannot_claim_cleanup(self):
        output = candidate()
        output['cleanup_state'] = 'released_unmaterialized'
        self.make(handler=lambda _: Result('completed', output, thread_id='owned', turn_id='turn'))
        result = self.engine.run_until_idle(self.run_id)
        self.assertFalse(result.get('task_cleanup'))
