from dataclasses import replace
import unittest

from kagebunshin.orchestration.domain.contracts import InvalidContract, Limits, Node, Plan, Rule
from kagebunshin.orchestration.domain.validation import audit_defects, check
from tests.unit.fakes import ROLES, candidate


class ContractsTest(unittest.TestCase):
    def test_reject_cycles_missing_dependencies_unknown_roles_and_types(self):
        variants = [
            (Node("a", "R07", "a", ("b",), {"b": "response.v1"}), Node("b", "R07", "b", ("a",), {"a": "response.v1"})),
            (Node("a", "R07", "a", ("missing",), {"missing": "response.v1"}),),
            (Node("a", "invented", "a"),),
            (Node("a", "R07", "a"), Node("b", "R07", "b", ("a",), {"a": "wrong.v9"})),
        ]
        for nodes in variants:
            with self.subTest(nodes=nodes), self.assertRaises(InvalidContract):
                Plan("task", nodes, {}).validate(ROLES)

    def test_resource_conflicts_require_dependency(self):
        a = Node("a", "R07", "a", writes=("workspace",))
        b = Node("b", "R07", "b", writes=("workspace",))
        with self.assertRaisesRegex(InvalidContract, "writers"):
            Plan("task", (a, b), {}).validate(ROLES)
        Plan("task", (a, replace(b, dependencies=("a",), input_types={"a": "response.v1"})), {}).validate(ROLES)

    def test_bool_and_nonfinite_limits_do_not_become_permission(self):
        for limit in [Limits(max_turns=True), Limits(max_concurrent=3), Limits(deadline_seconds=float("nan"))]:
            with self.subTest(limit=limit), self.assertRaises(InvalidContract):
                limit.validate()

    def test_numeric_checker_cannot_be_overridden_by_approved_field(self):
        node = Node("a", "R19", "mean", rules=(Rule("values.mean", "equals", 5),))
        self.assertTrue(check(node, {**candidate(999), "approved": True}))
        self.assertEqual(check(node, candidate(999))[0]["observed"], 999)
        self.assertTrue(check(node, candidate(True)))
        self.assertFalse(check(node, candidate()))
        self.assertFalse(check(node, candidate(5.0)))

    def test_required_and_unregistered_sources_are_defects(self):
        node = Node("a", "R07", "a", source_ids=("s1",))
        self.assertEqual({d["code"] for d in check(node, candidate(sources=["fake"]))}, {"source_unknown", "source_missing"})

    def test_audit_requires_typed_consistent_reasoned_result(self):
        for payload in [{"approved": True}, {"approved": True, "reason": "", "defects": []},
                        {"approved": False, "reason": "bad", "defects": []}]:
            self.assertTrue(audit_defects(payload))
        self.assertFalse(audit_defects({"approved": True, "reason": "checked", "defects": []}))

    def test_null_rule_requires_present_path(self):
        node = Node("a", "R39", "owner count", rules=(Rule("values.owner.count", "equals", None),))
        for values in ({}, {"owner": None}, {"owner": {}}, {"owner": []}):
            with self.subTest(values=values):
                payload = {**candidate(), "values": values}
                self.assertTrue(check(node, payload))
        self.assertFalse(check(node, {**candidate(), "values": {"owner": {"count": None}}}))
