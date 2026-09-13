import unittest
from dataclasses import replace
from kagebunshin.execution.domain.admission import Binding, Grant, Rejected, admit_batch


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self.binding = Binding('g', 'r', 'n', 'a', 'thread', 'turn', 3, 'workspace', 'c' * 64)
        self.grant = Grant(self.binding, frozenset({'apply_changes', 'read_files', 'run_check'}),
                           frozenset({'src/example.py'}), frozenset({'syntax'}), 20, 1000, 10000)
        self.proposal = {'call_id': 'call', 'operation': 'apply_changes',
                         'arguments': {'path': 'src/example.py', 'before_hash': None, 'content': 'x = 1\n'}}

    def test_oversized_batch_and_late_invalid_item_return_no_admission(self):
        proposals = [dict(self.proposal, call_id=str(i)) for i in range(900)]
        original = dict(self.proposal['arguments'])
        with self.assertRaises(Rejected):
            admit_batch(self.grant, self.binding, proposals, {}, now=10)
        proposals = [self.proposal, dict(self.proposal, call_id='last', operation='shell')]
        with self.assertRaises(Rejected):
            admit_batch(self.grant, self.binding, proposals, {}, now=10)
        self.assertEqual(self.proposal['arguments'], original)
        self.assertEqual(self.grant.remaining, 20)

    def test_replay_binding_and_immutable_payload(self):
        result = admit_batch(self.grant, self.binding, [self.proposal], {}, now=10)[0]
        existing = {'call': result.payload_hash}
        retry = admit_batch(replace(self.grant, remaining=0), self.binding, [self.proposal], existing, now=10)
        self.assertTrue(retry[0].replay)
        self.proposal['arguments']['content'] = 'changed'
        self.assertNotIn('changed', result.payload_json)
        with self.assertRaises(Rejected):
            admit_batch(self.grant, self.binding, [self.proposal], existing, now=10)
        self.assertEqual(existing, {'call': result.payload_hash})

    def test_scope_stop_path_and_deadline_reject(self):
        for binding in (replace(self.binding, turn_id='other'), replace(self.binding, thread_id='other'),
                        replace(self.binding, stop_epoch=2), replace(self.binding, workspace_id='other')):
            with self.subTest(binding=binding), self.assertRaises(Rejected):
                admit_batch(self.grant, binding, [self.proposal], {}, now=10)
        for path in ('../secret', '/secret', 'src/../secret', 'src//example.py', 'src/other.py'):
            with self.subTest(path=path), self.assertRaises(Rejected):
                admit_batch(self.grant, self.binding, [dict(self.proposal, arguments=dict(self.proposal['arguments'], path=path))], {}, now=10)
        for args in ({'now': 1000}, {'now': float('nan')}, {'now': 10, 'stopped': True}):
            with self.assertRaises(Rejected):
                admit_batch(self.grant, self.binding, [self.proposal], {}, **args)

    def test_invalid_host_grants_are_rejected(self):
        from kagebunshin.execution.domain.admission import validate_grant
        for grant in (replace(self.grant, deadline=float('inf')), replace(self.grant, remaining=True),
                      replace(self.grant, paths=frozenset({'../other'})),
                      replace(self.grant, operations=frozenset({'shell'})),
                      replace(self.grant, binding=replace(self.binding, stop_epoch=True))):
            with self.subTest(grant=grant), self.assertRaises(Rejected):
                validate_grant(grant,10)
