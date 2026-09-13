from pathlib import Path
from hashlib import sha256
import tempfile
import unittest
from multi_shadow_clone.execution.application.executor import Executor
from multi_shadow_clone.execution.application.reservations import Reservations
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.sqlite_store import SQLiteOperationStore
from multi_shadow_clone.execution.presentation.job_sessions import JobSessions, fingerprint
from multi_shadow_clone.execution.domain.admission import Rejected
from multi_shadow_clone.orchestration.ports import Request

class JobSessionsTest(unittest.TestCase):
    def test_real_workspace_grant_actual_turn_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'a').write_text('hello')
            files=OwnedFiles(root,{'a'})
            try:
                reservations=Reservations(SQLiteOperationStore(root/'operations.sqlite3'),lambda:10)
                sessions=JobSessions(reservations,Executor(reservations,{'workspace':files}),{'workspace':files.contract})
                scope=dict(node_id='node',stage='generate',role_id='R07',workspace_id='workspace',paths=['a'],checks=[],operations=['read_files'],max_calls=1,max_bytes=100)
                scope['runtime_hash']=fingerprint(sessions.scope_contract(scope))
                allowed=[True]
                request=Request('attempt','run','node','generate','R07','prompt','a'*64,may_continue=lambda:allowed[0])
                prepared=sessions.prepare(scope,request,100,0)
                prepared.bind('thread','turn')
                response=prepared.handle(dict(threadId='thread',turnId='turn',callId='call',tool='multi_shadow_clone_read_files',arguments=dict(files=[dict(path='a',expected_hash=sha256(b'hello').hexdigest())],max_bytes=100)))
                self.assertTrue(response['success'])
                allowed[0]=False
                with self.assertRaises(Rejected):sessions.prepare(scope,request,100,0)
                files.close()
                with self.assertRaises(Rejected):sessions.contract({'scopes':[scope]})
            finally:files.close()

    def test_fixed_tests_and_combined_write_check_scope_are_rejected(self):
        class Registry:
            def protected_paths(self):return {'check.js'}
            def contract(self,key):return {'template_hash':'a'*64}
        with tempfile.TemporaryDirectory() as directory:
            reservations=Reservations(SQLiteOperationStore(Path(directory)/'operations.sqlite3'),lambda:10)
            executor=Executor(reservations,{},writers={'workspace':object()})
            sessions=JobSessions(reservations,executor,{'workspace':lambda:{'paths':['check.js','code.js']}},attempt_checkers={'workspace':Registry()})
            base=dict(workspace_id='workspace',operations=['apply_changes'],paths=['check.js'],checks=[])
            with self.assertRaises(Rejected):sessions.scope_contract(base)
            base.update(paths=['code.js'],operations=['apply_changes','run_check'],checks=['test'])
            with self.assertRaises(Rejected):sessions.scope_contract(base)
            base.update(operations=['apply_changes'],checks=[])
            sessions.scope_contract(base)

    def test_unknown_check_recovery_uses_saved_checker_without_rerunning(self):
        from multi_shadow_clone.execution.domain.admission import Binding,Grant
        class Checker:
            def run_operation(self,*args,**kwargs):raise AssertionError('must not rerun')
            def completion(self,*args):return {'state':'completed','receipt':{'check_id':'test','definition_hash':'d'*64,'passed':True}}
        class Registry:
            restored=0
            def restore(self,*args):self.restored+=1;return Checker()
        with tempfile.TemporaryDirectory() as directory:
            store=SQLiteOperationStore(Path(directory)/'operations.sqlite3')
            reservations=Reservations(store,lambda:10)
            binding=Binding('attempt','run','node','attempt','thread','turn',0,'workspace','c'*64)
            reservations.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'test'}),1,100,100),check_contracts={'test':'d'*64})
            reservations.reserve(binding,[{'call_id':'call','operation':'run_check','arguments':{'check_id':'test'}}])
            reservations.begin(binding,'call',check_contract='d'*64)
            registry=Registry()
            sessions=JobSessions(reservations,Executor(reservations,{}),{},attempt_checkers={'workspace':registry})
            attempt=dict(id='attempt',node_id='node',thread_id='thread',turn_id='turn',binding_hash='c'*64)
            self.assertFalse(sessions.outcome('run',attempt)['complete'])
            result=sessions.recover_checks('run',attempt)
            self.assertTrue(result['complete']);self.assertEqual(result['passed_checks'],['test'])
            self.assertEqual(sessions.recover_checks('run',attempt),result)
            self.assertEqual(store.read('attempt')['grant']['remaining'],0)
            self.assertEqual(registry.restored,2)

    def test_shared_lease_blocks_other_jobs_and_fences_released_session(self):
        from dataclasses import replace
        from multi_shadow_clone.execution.infrastructure.workspace_leases import WorkspaceLeases
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'a').write_text('hello')
            files=OwnedFiles(root,{'a'})
            try:
                leases=WorkspaceLeases(root/'leases.sqlite3')
                reservations=Reservations(SQLiteOperationStore(root/'operations.sqlite3'),lambda:10)
                sessions=JobSessions(reservations,Executor(reservations,{'workspace':files}),{'workspace':files.contract},leases=leases)
                scope=dict(node_id='node',stage='generate',role_id='R07',workspace_id='workspace',paths=['a'],checks=[],operations=['read_files'],max_calls=1,max_bytes=100)
                scope['runtime_hash']=fingerprint(sessions.scope_contract(scope))
                request=Request('attempt','run','node','generate','R07','prompt','a'*64,may_continue=lambda:True)
                session=sessions.prepare(scope,request,100,0);session.bind('thread','turn')
                with self.assertRaises(Rejected):sessions.prepare(scope,replace(request,attempt_id='other',run_id='other'),100,0)
                contract=files.contract();identity={key:contract[key] for key in ('device','inode')}
                lease=leases.claim(identity,'run',fingerprint(contract))
                leases.release(identity,lease,'b'*64)
                with self.assertRaises(Rejected):session.handle(dict(threadId='thread',turnId='turn',callId='call',tool='multi_shadow_clone_read_files',arguments=dict(files=[dict(path='a',expected_hash=sha256(b'hello').hexdigest())],max_bytes=100)))
                self.assertEqual(reservations.store.read('attempt')['calls'],{})
            finally:files.close()
