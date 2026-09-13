import unittest

from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.application.operator import Operator
from multi_shadow_clone.orchestration.application.team import Team
from multi_shadow_clone.orchestration.domain.contracts import Node, Plan
from tests.unit.fakes import MemoryStore, ROLES, ScriptedProvider


class Jobs:
    def start(self, run_id, action, cancel):
        self.action, self.cancel = action, cancel
        return {"id": run_id, "state": "accepted_for_execution"}
    def status(self): return {"state": "idle"}


class OperatorTest(unittest.TestCase):
    def test_usage_snapshot_marks_age_without_dispatch(self):
        provider = ScriptedProvider()
        provider.usage_snapshot = lambda: {"available": True, "scope": "account", "observed_at": 900, "used_percent": 25}
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000)
        view = Operator(engine, Team(engine), Jobs(), lambda: 1000).snapshot()
        self.assertTrue(view["usage"]["stale"])
        self.assertEqual(view["usage"]["used_percent"], 25)
        self.assertEqual(provider.requests, [])

    def test_preflight_pause_has_recovery_guidance_and_resume_still_dispatches_nothing(self):
        from multi_shadow_clone.orchestration.ports import ProviderBlocked
        provider = ScriptedProvider()
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000)
        job = engine.create(Plan("fixture", (Node("a", "R07", "read"),), {}))
        def blocked(): raise ProviderBlocked("unknown ordinary usage")
        provider.preflight = blocked
        engine.run_until_idle(job)
        operator = Operator(engine, Team(engine), Jobs(), lambda: 1000)
        view = operator.snapshot()["jobs"][0]
        self.assertTrue(view["can_resume"])
        self.assertIn("条件を確認できるまで送信しません", view["next_step"])
        with self.assertRaises(ValueError): operator.act("run", job)
        operator.act("resume", job)
        self.assertEqual(engine.run_until_idle(job)["state"], "blocked_preflight")
        self.assertEqual(provider.requests, [])

    def test_form_saves_bounded_request_without_dispatch_or_mutating_input(self):
        provider = ScriptedProvider()
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000)
        operator = Operator(engine, Team(engine), Jobs(), lambda: 1000)
        result = operator.submit("  explain fixture  ", "<untrusted>資料</untrusted>", 8)
        run = engine.status(result["id"])
        self.assertEqual(provider.requests, [])
        self.assertEqual(run["plan"]["objective"], "explain fixture")
        self.assertEqual(run["planning"]["sources"], {"userMaterial": "<untrusted>資料</untrusted>"})
        self.assertEqual(run["plan"]["limits"]["max_turns"], 8)
        for args in [("", "", 8), ("a", "", True), ("a", "", 21), ("a", "x" * 12001, 8)]:
            with self.assertRaises(ValueError): operator.submit(*args)
        self.assertEqual(len(engine.list_runs()), 1)

    def test_old_build_cannot_run_through_operator_and_result_is_hidden(self):
        from tests.unit.test_build_binding import Build
        build = Build()
        engine = Engine(MemoryStore(), ScriptedProvider(), ROLES, lambda: 1000, build_identity=build)
        job = engine.create(Plan("fixture", (Node("a", "R07", "read"),), {}))
        engine.run_until_idle(job)
        operator = Operator(engine, Team(engine), Jobs(), lambda: 1000)
        self.assertEqual(len(operator.snapshot()["jobs"][0]["results"]), 1)
        build.version = "new-checker-code"
        with self.assertRaises(ValueError): operator.act("run", job)
        view = operator.snapshot()["jobs"][0]
        self.assertEqual(view["state"], "completed")
        self.assertFalse(view["acceptance"]["compatible"])
        self.assertEqual(view["results"], [])

    def test_read_only_snapshot_and_explicit_action_share_engine(self):
        provider = ScriptedProvider()
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000)
        job = engine.create(Plan("fixture", (Node("a", "R07", "read"),), {}))
        jobs = Jobs()
        operator = Operator(engine, Team(engine), jobs, lambda: 1000)
        self.assertEqual(operator.snapshot()["live_roles"], 0)
        self.assertEqual(provider.requests, [])
        operator.act("run", job)
        self.assertEqual(provider.requests, [])
        jobs.action()
        self.assertEqual(engine.status(job)["state"], "completed")
        operator.act("stop", job)
        self.assertTrue(engine.status(job)["stopped"])
        operator.act("resume", job)
        self.assertFalse(engine.status(job)["stopped"])
        with self.assertRaises(ValueError): operator.act("delete", job)

    def test_cleanup_action_and_projection_do_not_generate_again(self):
        from multi_shadow_clone.orchestration.ports import Result
        from tests.unit.fakes import candidate
        provider = ScriptedProvider(lambda _: Result('completed', candidate(), 'owned', 'turn'))
        def unavailable(*args): raise RuntimeError('archive unavailable')
        provider.archive_owned = unavailable
        engine = Engine(MemoryStore(), provider, ROLES, lambda: 1000)
        run = engine.create(Plan('fixture', (Node('a', 'R07', 'read'),), {}))
        jobs = Jobs()
        operator = Operator(engine, Team(engine), jobs, lambda: 1000)
        with self.assertRaises(ValueError): operator.act('cleanup', run)
        engine.run_until_idle(run)
        view = operator.snapshot()['jobs'][0]
        self.assertTrue(view['can_cleanup'])
        self.assertEqual(view['cleanup'][0]['state'], 'cleanup_pending')
        count = len(provider.requests)
        provider.archive_owned = lambda *args: None
        operator.act('cleanup', run)
        jobs.action()
        view = operator.snapshot()['jobs'][0]
        self.assertFalse(view['can_cleanup'])
        self.assertEqual(view['cleanup'][0]['state'], 'archived')
        self.assertEqual(len(provider.requests), count)

    def test_workspace_retirement_action_and_resume_state_use_engine_boundary(self):
        class Sessions:
            def retire(self,run):return {'leases':{}}
        engine=Engine(MemoryStore(),ScriptedProvider(),ROLES,lambda:1000,tool_sessions=Sessions())
        job=engine.create(Plan('fixture',(Node('a','R07','read'),),{}))
        jobs=Jobs();operator=Operator(engine,Team(engine),jobs,lambda:1000)
        operator.act('retire-workspace',job)
        jobs.action()
        view=operator.snapshot()['jobs'][0]
        self.assertEqual(view['workspace_retirement']['state'],'retired')
        self.assertIn('この仕事は再開できません',view['next_step'])
        self.assertFalse(view['can_resume'])
        self.assertFalse(view['can_retire_workspace'])
        with self.assertRaises(ValueError):operator.act('resume',job)
