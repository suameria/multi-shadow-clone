import unittest

from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.application.overview import Overview
from multi_shadow_clone.orchestration.domain.contracts import Node, Plan
from multi_shadow_clone.orchestration.ports import Result
from tests.unit.fakes import MemoryStore, ROLES, ScriptedProvider, candidate


class OverviewTest(unittest.TestCase):
    def test_expired_or_stopped_worker_is_never_shown_live(self):
        now = [1000]
        store = MemoryStore()
        provider = ScriptedProvider(lambda req: Result("unknown"))
        engine = Engine(store, provider, ROLES, lambda: now[0])
        job = engine.create(Plan("synthetic", (Node("a", "R07", "a"),), {}))
        engine.step(job)
        run = store.read(job)
        run["attempts"][0].update(state="running", heartbeat_at=1000)
        store.save(run, run["revision"])
        overview = Overview(engine, lambda: now[0])
        self.assertEqual(overview.snapshot()["live_roles"], 1)
        now[0] = 1016
        self.assertEqual(overview.snapshot()["live_roles"], 0)
        self.assertEqual(overview.snapshot()["unknown_attempts"], 1)
        now[0] = 1001
        engine.stop(job)
        self.assertEqual(overview.snapshot()["live_roles"], 0)
        self.assertEqual(overview.snapshot()["jobs"][0]["active"][0]["activity"], "stopping")

    def test_completed_candidate_role_is_idle_and_not_promoted_to_expert(self):
        engine = Engine(MemoryStore(), ScriptedProvider(), ROLES, lambda: 1000)
        job = engine.create(Plan("synthetic", (Node("a", "R07", "a"),), {}))
        engine.run_until_idle(job)
        view = Overview(engine, lambda: 1000).snapshot()
        self.assertEqual(view["jobs"][0]["state"], "completed")
        role = next(r for r in view["roles"] if r["id"] == "R07")
        self.assertEqual(role["activity"], "idle")
        self.assertEqual(role["lifecycle"], "candidate")
