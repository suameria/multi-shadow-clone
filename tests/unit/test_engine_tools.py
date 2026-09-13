import unittest
from copy import deepcopy
from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.domain.contracts import Plan, Node, InvalidContract
from tests.unit.fakes import MemoryStore, ScriptedProvider, ROLES

class Session:
    def validate_request(self, *args): self.binding = args

class Sessions:
    version = 'runtime-v1'
    def __init__(self): self.prepared = []
    def outcome(self, run_id, attempt): return {'complete': True, 'passed': True, 'calls': {}}
    def contract(self, policy): return {'runtime': self.version, 'policy': deepcopy(policy)}
    def prepare(self, scope, request, deadline, stop_epoch):
        self.prepared.append((scope, request, deadline, stop_epoch))
        return Session()

class EngineToolsTest(unittest.TestCase):
    def setUp(self):
        self.store, self.provider, self.sessions = MemoryStore(), ScriptedProvider(), Sessions()
        self.engine = Engine(self.store, self.provider, ROLES, lambda: 1000, tool_sessions=self.sessions)
        self.plan = Plan('fixture', (Node('work', 'R07', 'read', audit_role='R12', max_attempts=2),), {})
        self.policy = dict(version=1, max_calls=2, scopes=[dict(node_id='work', stage='generate', role_id='R07',
            workspace_id='owned', runtime_hash='a'*64, operations=['read_files'], paths=['a.txt'], checks=[], max_calls=1, max_bytes=256)])

    def test_only_explicit_stage_gets_tools_and_saved_result_is_bound(self):
        job = self.engine.create(self.plan, execution_policy=self.policy)
        self.policy['scopes'][0]['paths'].append('not-granted')
        self.engine.run_until_idle(job)
        self.assertEqual(self.engine.status(job)['state'], 'completed')
        self.assertIsNotNone(self.provider.requests[0].tool_session)
        self.assertIsNone(self.provider.requests[1].tool_session)
        self.assertEqual(self.sessions.prepared[0][0]['paths'], ['a.txt'])
        self.assertNotIn('Do not call tools', self.provider.requests[0].prompt)
        self.engine.accepted_result(job, 'work')
        self.sessions.version = 'changed'
        with self.assertRaises(InvalidContract): self.engine.accepted_result(job, 'work')

    def test_restart_missing_adapter_or_changed_policy_stops_before_dispatch(self):
        job = self.engine.create(self.plan, execution_policy=self.policy)
        restarted = Engine(self.store, self.provider, ROLES, lambda:1000)
        restarted.step(job)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(restarted.status(job)['state'], 'blocked_contract_changed')
        job = self.engine.create(self.plan, execution_policy=self.policy)
        self.store.data[job]['execution_policy']['scopes'][0]['max_calls'] = 2
        self.engine.step(job)
        self.assertEqual(self.provider.requests, [])

    def test_no_implicit_tools_and_failed_prepare_never_dispatches(self):
        job = self.engine.create(self.plan)
        self.engine.run_until_idle(job)
        self.assertTrue(all(r.tool_session is None for r in self.provider.requests))
        self.assertEqual(self.sessions.prepared, [])
        self.provider.requests.clear()
        job = self.engine.create(self.plan, execution_policy=self.policy)
        self.sessions.prepare = lambda *args: None
        self.engine.step(job)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.engine.status(job)['attempts'][0]['terminal_status'], 'not_sent')

    def test_unknown_operations_hold_reservation_despite_completed_model(self):
        self.sessions.outcome=lambda *args: {'complete':False,'passed':False,'calls':{'write':{'state':'unknown'}}}
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.step(job)
        saved=self.engine.status(job)
        self.assertEqual(saved['nodes']['work']['state'],'unknown')
        self.assertIsNotNone(saved['nodes']['work']['active'])
        self.assertEqual(len(self.provider.requests),1)
        with self.assertRaises(InvalidContract):self.engine.accepted_result(job,'work')

    def test_changed_operation_receipt_invalidates_accepted_result(self):
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.run_until_idle(job)
        self.engine.accepted_result(job,'work')
        self.sessions.outcome=lambda *args: {'complete':True,'passed':True,'calls':{'changed':{}}}
        with self.assertRaises(InvalidContract):self.engine.accepted_result(job,'work')

    def test_auditor_receives_host_evidence_and_missing_record_invalidates_acceptance(self):
        import json
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.run_until_idle(job)
        audit=json.loads(self.provider.requests[1].prompt.split('DATA_JSON\n')[1])
        evidence=audit['contract']['host_operation_evidence']
        self.assertEqual(len(evidence),1)
        self.assertEqual(evidence[0]['outcome']['calls'],{})
        del self.store.data[job]['attempts'][0]['tool_outcome']
        with self.assertRaises(InvalidContract):self.engine.accepted_result(job,'work')

    def test_required_check_cannot_be_replaced_by_model_completion_or_empty_calls(self):
        scope=self.policy['scopes'][0]
        scope.update(operations=['run_check'],paths=[],checks=['arithmetic'],required_checks=['arithmetic'])
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.step(job)
        self.assertEqual(self.engine.status(job)['attempts'][0]['terminal_status'],'tool_failed')
        self.assertEqual(len(self.provider.requests),1)
        with self.assertRaises(InvalidContract):self.engine.accepted_result(job,'work')
        self.sessions.outcome=lambda *args: {'complete':True,'passed':True,'calls':{},'passed_checks':['arithmetic']}
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.run_until_idle(job)
        self.engine.accepted_result(job,'work')

    def test_missing_check_repair_is_bounded_and_failed_history_is_preserved(self):
        scope=self.policy['scopes'][0]
        scope.update(operations=['run_check'],paths=[],checks=['required'],required_checks=['required'])
        observed={}
        def outcome(run_id,attempt):
            if attempt['id'] not in observed:
                observed[attempt['id']]={'complete':True,'passed':True,'calls':{},
                    'passed_checks':[] if not observed else ['required']}
            return observed[attempt['id']]
        self.sessions.outcome=outcome
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.run_until_idle(job)
        self.assertEqual(self.engine.status(job)['state'],'completed')
        self.assertEqual(self.engine.status(job)['attempts'][0]['terminal_status'],'tool_failed')
        self.assertEqual(len(self.provider.requests),3)
        self.engine.accepted_result(job,'work')

    def test_identical_missing_check_stops_without_unbounded_new_turns(self):
        scope=self.policy['scopes'][0]
        scope.update(operations=['run_check'],paths=[],checks=['required'],required_checks=['required'])
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.run_until_idle(job)
        self.assertEqual(self.engine.status(job)['nodes']['work']['state'],'no_progress')
        self.assertEqual(len(self.provider.requests),2)

    def test_reconcile_recovers_check_without_new_model_dispatch(self):
        from kagebunshin.orchestration.ports import Result
        from tests.unit.fakes import candidate
        scope=self.policy['scopes'][0]
        scope.update(operations=['run_check'],paths=[],checks=['required'],required_checks=['required'])
        ready=[False]
        self.sessions.outcome=lambda *args:{'complete':ready[0],'passed':ready[0],'calls':{},'passed_checks':['required'] if ready[0] else []}
        self.sessions.recover_checks=lambda *args:ready.__setitem__(0,True)
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.step(job)
        self.assertEqual(self.engine.status(job)['nodes']['work']['state'],'unknown')
        self.provider.reconciled=Result('completed',candidate())
        self.engine.reconcile(job)
        self.assertEqual(self.engine.status(job)['nodes']['work']['state'],'audit_pending')
        self.assertEqual(len(self.provider.requests),1)

    def test_tool_failed_terminal_tasks_are_eligible_for_archive(self):
        from kagebunshin.orchestration.ports import Result
        from tests.unit.fakes import candidate
        scope=self.policy['scopes'][0]
        scope.update(operations=['run_check'],paths=[],checks=['required'],required_checks=['required'])
        def handle(request):
            request.progress('thread_'+request.attempt_id,'turn')
            return Result('completed',candidate())
        self.provider.handler=handle
        archived=[]
        self.provider.archive_owned=lambda thread,turn:archived.append((thread,turn))
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.run_until_idle(job)
        self.engine.cleanup(job)
        self.assertEqual(len(archived),2)
        self.engine.cleanup(job)
        self.assertEqual(len(archived),2)

    def test_retirement_failure_stays_fenced_and_retry_saves_receipt(self):
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.run_until_idle(job)
        def failed(run):
            self.assertTrue(run['stopped'])
            self.assertEqual(run['workspace_retirement']['state'],'retiring')
            raise OSError('receipt not saved')
        self.sessions.retire=failed
        with self.assertRaises(OSError):self.engine.retire_workspace(job)
        with self.assertRaises(InvalidContract):self.engine.resume(job)
        self.assertFalse(self.engine.step(job))
        calls=[]
        self.sessions.retire=lambda run:calls.append(run['id']) or {'retired':True}
        saved=self.engine.retire_workspace(job)
        self.assertEqual(saved['workspace_retirement']['state'],'retired')
        self.assertEqual(self.engine.retire_workspace(job),saved)
        self.assertEqual(calls,[job])
        with self.assertRaises(InvalidContract):self.engine.resume(job)

    def test_retirement_refuses_unknown_provider_attempt(self):
        from kagebunshin.orchestration.ports import Result
        self.provider.handler=lambda request:Result('unknown')
        job=self.engine.create(self.plan,execution_policy=self.policy)
        self.engine.step(job)
        with self.assertRaises(InvalidContract):self.engine.retire_workspace(job)
        self.assertNotIn('workspace_retirement',self.engine.status(job))
