import json
import unittest

from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.application.team import Team
from multi_shadow_clone.orchestration.domain.contracts import Limits
from multi_shadow_clone.orchestration.ports import Result
from tests.unit.fakes import MemoryStore, ROLES, ScriptedProvider, candidate


class PlanningTest(unittest.TestCase):
    def setup_team(self, proposal, limits=Limits()):
        def handler(request):
            if request.node_id == "mainPlanner":
                return Result("completed", {**candidate(sources=["s1", "multiShadowCloneRoleIndex"]), "values": {"nodes": proposal}})
            if request.stage == "audit":
                return Result("completed", {"approved": True, "reason": "test evidence", "defects": []})
            return Result("completed", candidate(sources=["s1"]))
        self.provider = ScriptedProvider(handler)
        self.engine = Engine(MemoryStore(), self.provider, ROLES, lambda: 1000)
        self.team = Team(self.engine)
        self.run_id = self.team.submit("calculate mean", {"s1": "synthetic values"}, limits)

    def node(self, **overrides):
        return {"id": "stats", "role_id": "R19", "instruction": "calculate", "dependencies": [], "source_ids": ["s1"], **overrides}

    def test_main_selects_only_needed_role_and_all_turns_are_counted(self):
        self.setup_team([self.node()])
        result = self.team.advance(self.run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual([r.role_id for r in self.provider.requests], ["R01", "R19", "R12"])
        self.assertEqual(result["turns"], 3)
        self.assertEqual(result["planning"]["stage"], "admitted")

    def test_main_cannot_grant_write_permission_or_invent_roles(self):
        for proposal in [[self.node(writes=["somewhere"])], [self.node(role_id="madeUp")], [], [self.node(source_ids=["newSource"])]]:
            with self.subTest(proposal=proposal):
                self.setup_team(proposal)
                result = self.team.advance(self.run_id)
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(len(self.provider.requests), 1 if proposal == [] else 2)
                self.assertEqual({r.role_id for r in self.provider.requests}, {"R01"})

    def test_planner_repairs_missing_citation_without_resetting_budget(self):
        self.setup_team([self.node()])
        normal = self.provider.handler
        calls = []
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return Result("completed", {**candidate(sources=["s1"]), "values": {"nodes": [self.node()]}})
            return normal(request)
        self.provider.handler = handle
        result = self.team.advance(self.run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["turns"], 4)
        repair = json.loads(calls[1].prompt.split("DATA_JSON\n")[1])
        self.assertEqual(repair["defects_to_repair"][0]["code"], "source_missing")
        self.assertEqual(result["planning"]["planner_result"]["attempts"], 2)

    def test_planner_repairs_invalid_dag_before_admission(self):
        self.setup_team([self.node()])
        normal = self.provider.handler
        calls = []
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return Result("completed", {**candidate(sources=["s1", "multiShadowCloneRoleIndex"]),
                                            "values": {"nodes": [self.node(dependencies=["missing"])]}})
            return normal(request)
        self.provider.handler = handle
        result = self.team.advance(self.run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual([r.role_id for r in calls], ["R01", "R01", "R19", "R12"])
        repair = json.loads(calls[1].prompt.split("DATA_JSON\n")[1])
        self.assertEqual(repair["defects_to_repair"][0]["code"], "plan_contract")

    def test_planning_does_not_reset_turn_budget_or_deadline(self):
        self.setup_team([self.node()], Limits(max_turns=2))
        result = self.team.advance(self.run_id)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["deadline"], 1900)
        self.assertNotIn("stats", result["nodes"])
        self.assertEqual({r.role_id for r in self.provider.requests}, {"R01"})

    def test_overdelegation_repairs_before_any_specialist_dispatch(self):
        self.setup_team([self.node()], Limits(max_turns=5))
        normal = self.provider.handler
        calls = []
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                nodes = [self.node(id="a"), self.node(id="b", dependencies=["a"]), self.node(id="c", dependencies=["b"])]
                return Result("completed", {**candidate(sources=["s1", "multiShadowCloneRoleIndex"]), "values": {"nodes": nodes}})
            return normal(request)
        self.provider.handler = handle
        result = self.team.advance(self.run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["turns"], 4)
        self.assertEqual(calls[0].output_schema["properties"]["values"]["properties"]["nodes"]["maxItems"], 2)
        self.assertEqual(calls[1].output_schema["properties"]["values"]["properties"]["nodes"]["maxItems"], 1)
        self.assertEqual([r.role_id for r in calls], ["R01", "R01", "R19", "R12"])
        feedback = json.loads(calls[1].prompt.split("DATA_JSON\n")[1])["defects_to_repair"]
        self.assertIn("only 4 remain", feedback[0]["message"] if "message" in feedback[0] else str(feedback[0]))
