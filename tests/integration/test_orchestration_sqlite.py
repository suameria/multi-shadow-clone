from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.domain.contracts import Node, Plan
from multi_shadow_clone.orchestration.infrastructure.sqlite_store import SQLiteRunStore
from multi_shadow_clone.orchestration.ports import Conflict, Result
from tests.unit.fakes import ROLES, ScriptedProvider, candidate


class SQLiteIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteRunStore(self.path)
        self.provider = ScriptedProvider()
        self.engine = Engine(self.store, self.provider, ROLES, lambda: 1000)

    def test_persists_receipts_events_and_stop_across_adapter_restart(self):
        run_id = self.engine.create(Plan("task", (Node("a", "R07", "a"),), {}))
        self.engine.stop(run_id)
        restarted = Engine(SQLiteRunStore(self.path), self.provider, ROLES, lambda: 1000)
        self.assertFalse(restarted.step(run_id))
        restarted.resume(run_id)
        result = restarted.run_until_idle(run_id)
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["nodes"]["a"]["receipt"]["artifact_hash"])
        self.assertEqual([e["seq"] for e in result["events"]], list(range(1, len(result["events"]) + 1)))

    def test_stale_snapshot_cannot_undo_stop(self):
        run_id = self.engine.create(Plan("task", (Node("a", "R07", "a"),), {}))
        old = self.store.read(run_id)
        self.engine.stop(run_id)
        with self.assertRaises(Conflict):
            self.store.save(old, old["revision"])
        self.assertTrue(self.store.read(run_id)["stopped"])

    def test_event_mutation_rolls_back_entire_transaction(self):
        run_id = self.engine.create(Plan("task", (Node("a", "R07", "a"),), {}))
        old = self.store.read(run_id)
        changed = self.store.read(run_id)
        changed["events"][0]["kind"] = "fake_complete"
        changed["state"] = "completed"
        with self.assertRaises(ValueError):
            self.store.save(changed, old["revision"])
        self.assertEqual(self.store.read(run_id), old)

    def test_two_workers_claim_different_nodes_and_third_is_capped(self):
        entered = threading.Barrier(3)
        release = threading.Event()
        def handler(request):
            entered.wait(timeout=5)
            release.wait(timeout=5)
            return Result("completed", candidate())
        self.provider.handler = handler
        run_id = self.engine.create(Plan("task", tuple(Node(n, "R07", n) for n in ["a", "b", "c"]), {}))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.engine.step, run_id)
            second = pool.submit(self.engine.step, run_id)
            try:
                entered.wait(timeout=5)
                self.assertFalse(self.engine.step(run_id))
                self.assertEqual(len(self.provider.requests), 2)
                self.assertEqual(len({r.node_id for r in self.provider.requests}), 2)
            finally:
                release.set()
            self.assertTrue(first.result(timeout=5))
            self.assertTrue(second.result(timeout=5))
        self.assertEqual(sum(s["state"] == "accepted" for s in self.store.read(run_id)["nodes"].values()), 2)

    def test_distinct_runs_share_the_same_two_slot_limit(self):
        entered = threading.Barrier(3)
        release = threading.Event()
        def handler(request):
            entered.wait(timeout=5)
            release.wait(timeout=5)
            return Result("completed", candidate())
        self.provider.handler = handler
        jobs = [self.engine.create(Plan("task", (Node("a", "R07", "a"),), {})) for _ in range(3)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(self.engine.step, job) for job in jobs[:2]]
            try:
                entered.wait(timeout=5)
                another_process_store = SQLiteRunStore(self.path)
                another = Engine(another_process_store, self.provider, ROLES, lambda: 1000)
                self.assertFalse(another.step(jobs[2]))
                self.assertEqual(another.status(jobs[2])["turns"], 0)
                self.assertEqual(len(self.provider.requests), 2)
            finally:
                release.set()
            for task in tasks:
                self.assertTrue(task.result(timeout=5))
        self.provider.handler = None
        self.assertEqual(self.engine.run_until_idle(jobs[2])["state"], "completed")

    def test_repairs_and_independent_audits_share_budget_under_real_worker_race(self):
        from multi_shadow_clone.orchestration.domain.contracts import Limits, Rule
        from multi_shadow_clone.orchestration.infrastructure.workers import Workers
        counts = {}
        def handler(request):
            if request.stage == "audit":
                return Result("completed", {"approved": True, "reason": "fixture", "defects": []})
            counts[request.node_id] = counts.get(request.node_id, 0) + 1
            return Result("completed", candidate(999 if request.node_id == "a" and counts["a"] == 1 else 5))
        self.provider.handler = handler
        job = self.engine.create(Plan("bounded parallel repairs", (
            Node("a", "R07", "repair", rules=(Rule("values.mean", "equals", 5),), audit_role="R12"),
            Node("b", "R07", "independent", audit_role="R12"),
        ), {}, Limits(max_turns=4, max_concurrent=2)))
        result = Workers(self.engine).run(job)
        self.assertEqual(result["state"], "paused_budget")
        self.assertEqual(result["turns"], 4)
        self.assertEqual(len(self.provider.requests), 4)
        self.assertEqual(counts["b"], 1)
        self.assertEqual(sum(a["state"] != "terminal" for a in result["attempts"]), 0)
