from dataclasses import asdict, replace
import unittest

from multi_shadow_clone.orchestration.domain.contracts import Node, Plan, plan_from_dict, InvalidContract
from multi_shadow_clone.orchestration.domain.validation import check
from multi_shadow_clone.orchestration.domain.output_format import response_schema
from multi_shadow_clone.orchestration.application.prompts import binding, context
from tests.unit.fakes import ROLES


class OutputFormatTest(unittest.TestCase):
    def test_saved_contract_and_binding_include_declared_types(self):
        node = Node('a', 'R07', 'analyze', response_fields={'count': 'number'})
        plan = Plan('task', (node,), {})
        plan.validate(ROLES)
        restored = plan_from_dict(asdict(plan))
        self.assertEqual(restored.nodes[0].response_fields, {'count': 'number'})
        before = binding(context(plan, node, ROLES['R07'], {}), None)
        changed = replace(node, response_fields={'count': 'string'})
        self.assertNotEqual(before, binding(context(plan, changed, ROLES['R07'], {}), None))
        with self.assertRaises(InvalidContract):
            replace(plan, nodes=(replace(node, response_fields={'count': 'execute'}),)).validate(ROLES)

    def test_local_validation_rejects_missing_extra_and_bool_as_number(self):
        node = Node('a', 'R07', 'analyze', response_fields={'count': 'number', 'unknown': 'string or null'})
        for values in ({'count': True, 'unknown': None}, {'count': 3}, {'count': 3, 'unknown': None, 'extra': 1}):
            defects = check(node, {'text': 'candidate', 'source_ids': [], 'limits': [], 'values': values})
            self.assertIn('value_types', [d['code'] for d in defects])
        self.assertEqual(check(node, {'text': 'candidate', 'source_ids': [], 'limits': [],
                                    'values': {'count': 3, 'unknown': None}}), [])
        self.assertIsNone(response_schema({}))
