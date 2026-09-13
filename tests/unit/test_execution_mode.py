import unittest

from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.domain.contracts import Node, Plan
from tests.unit.fakes import MemoryStore, ROLES, ScriptedProvider


class ExecutionModeTest(unittest.TestCase):
    def test_offline_job_cannot_be_resumed_as_subscription_job(self):
        store = MemoryStore()
        fixture, live_double = ScriptedProvider(), ScriptedProvider()
        fixture.contract = lambda: {"kind": "offline-fixture"}
        live_double.contract = lambda: {"kind": "codex-subscription", "model": "test-model"}
        first = Engine(store, fixture, ROLES, lambda: 1000)
        run_id = first.create(Plan("fixture", (Node("a", "R07", "draft"),), {}))
        resumed = Engine(store, live_double, ROLES, lambda: 1000)
        self.assertEqual(resumed.run_until_idle(run_id)["state"], "blocked_contract_changed")
        self.assertEqual(live_double.requests, [])
