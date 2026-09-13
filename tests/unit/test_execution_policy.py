import unittest
from copy import deepcopy
from dataclasses import replace

from multi_shadow_clone.orchestration.domain.contracts import InvalidContract, Node, Plan
from multi_shadow_clone.orchestration.domain.execution_policy import validate_policy


class ExecutionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.plan = Plan('fix', (Node('fix', 'R20', 'repair', audit_role='R12', max_attempts=2),), {})
        self.scope = dict(node_id='fix', stage='generate', role_id='R20', workspace_id='owned',
                          runtime_hash='a' * 64, operations=['apply_changes'], paths=['src/a.js'],
                          checks=[], max_calls=2, max_bytes=4096)
        self.policy = dict(version=1, max_calls=4, scopes=[self.scope])

    def test_contract_is_detached_and_retries_count_toward_job_bound(self):
        saved = validate_policy(self.policy, self.plan)
        self.scope['paths'].append('other')
        self.assertEqual(saved['scopes'][0]['paths'], ['src/a.js'])
        self.policy['max_calls'] = 3
        with self.assertRaises(InvalidContract):
            validate_policy(self.policy, self.plan)

    def test_role_names_and_model_plans_cannot_grant_authority(self):
        for change in ({'node_id': 'other'}, {'stage': 'audit'}, {'role_id': 'R12'},
                       {'operations': ['shell']}, {'runtime_hash': 'unverified'}):
            policy = deepcopy(self.policy)
            policy['scopes'][0].update(change)
            with self.subTest(change=change), self.assertRaises(InvalidContract):
                validate_policy(policy, self.plan)
        plan = replace(self.plan, nodes=(replace(self.plan.nodes[0], response_format='plan'),))
        with self.assertRaises(InvalidContract):
            validate_policy(self.policy, plan)

    def test_malformed_paths_budgets_and_duplicate_scopes_reject(self):
        for change in ({'paths': ['../a']}, {'paths': ['/tmp/a']}, {'paths': []},
                       {'paths': ['a', 'a']}, {'checks': ['ungranted']},
                       {'max_calls': True}, {'max_bytes': 0}, {'stage': []}):
            policy = deepcopy(self.policy)
            policy['scopes'][0].update(change)
            with self.subTest(change=change), self.assertRaises(InvalidContract):
                validate_policy(policy, self.plan)
        self.policy['scopes'].append(deepcopy(self.scope))
        with self.assertRaises(InvalidContract):
            validate_policy(self.policy, self.plan)

    def test_auditor_check_is_separate_explicit_scope(self):
        self.scope.update(stage='audit', role_id='R12', operations=['run_check'], paths=[], checks=['arithmetic'])
        self.assertEqual(validate_policy(self.policy, self.plan), self.policy)

    def test_unordered_workspace_read_write_and_check_conflicts_reject(self):
        other=replace(self.plan.nodes[0],id='inspect')
        plan=replace(self.plan,nodes=(*self.plan.nodes,other))
        right=deepcopy(self.scope)
        right.update(node_id='inspect',operations=['read_files'])
        policy=dict(version=1,max_calls=8,scopes=[self.scope,right])
        with self.assertRaises(InvalidContract):validate_policy(policy,plan)
        ordered=replace(plan,nodes=(plan.nodes[0],replace(other,dependencies=('fix',))))
        validate_policy(policy,ordered)
        right.update(operations=['run_check'],paths=[],checks=['registered'])
        with self.assertRaises(InvalidContract):validate_policy(policy,plan)
        validate_policy(policy,ordered)

    def test_required_checks_must_fit_granted_ids_and_budget(self):
        self.scope.update(operations=['run_check'],paths=[],checks=['one','two'],required_checks=['one','two'],max_calls=1)
        with self.assertRaises(InvalidContract):validate_policy(self.policy,self.plan)
        self.scope.update(max_calls=2,required_checks=['outside'])
        with self.assertRaises(InvalidContract):validate_policy(self.policy,self.plan)
        self.scope['required_checks']=['one']
        validate_policy(self.policy,self.plan)
