from pathlib import Path
from hashlib import sha256
import tempfile
import unittest

from multi_shadow_clone.bootstrap import owned_job_sessions
from multi_shadow_clone.execution.presentation.job_sessions import fingerprint
from multi_shadow_clone.orchestration.application.engine import Engine
from multi_shadow_clone.orchestration.domain.contracts import Node, Plan
from multi_shadow_clone.orchestration.infrastructure.sqlite_store import SQLiteRunStore
from multi_shadow_clone.orchestration.ports import Result
from tests.unit.fakes import ROLES, ScriptedProvider, candidate


class EngineWorkspaceTest(unittest.TestCase):
    def test_saved_engine_job_writes_then_audits_and_reopens_without_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            workspace=root/'workspace';workspace.mkdir()
            (workspace/'a.js').write_text('before')
            state=root/'private'
            store=SQLiteRunStore(state/'jobs.sqlite3')
            calls=[]
            def execute(request):
                calls.append(request.stage)
                if request.stage=='audit':
                    self.assertIsNone(request.tool_session)
                    return Result('completed',{'approved':True,'reason':'fixture audit','defects':[]})
                session=request.tool_session
                request.progress('thread','turn')
                session.bind('thread','turn')
                result=session.handle(dict(threadId='thread',turnId='turn',callId='write',
                    tool='multi_shadow_clone_apply_changes',arguments=dict(path='a.js',
                    before_hash=sha256(b'before').hexdigest(),content='after')))
                self.assertTrue(result['success'])
                return Result('completed',candidate())
            provider=ScriptedProvider(execute)
            options=dict(lease_path=state/'leases.sqlite3',data_dir=state,workspace_id='owned',workspace_root=workspace,paths=['a.js'],clock=lambda:1000)
            with owned_job_sessions(**options) as sessions:
                scope=dict(node_id='work',stage='generate',role_id='R07',workspace_id='owned',
                    operations=['apply_changes'],paths=['a.js'],checks=[],max_calls=1,max_bytes=100)
                scope['runtime_hash']=fingerprint(sessions.scope_contract(scope))
                engine=Engine(store,provider,ROLES,lambda:1000,tool_sessions=sessions)
                job=engine.create(Plan('fixture',(Node('work','R07','change',audit_role='R12',max_attempts=1),),{}),
                    execution_policy=dict(version=1,max_calls=1,scopes=[scope]))
                engine.run_until_idle(job)
                self.assertEqual(engine.status(job)['state'],'completed')
                self.assertEqual((workspace/'a.js').read_text(),'after')
                engine.accepted_result(job,'work')
            with owned_job_sessions(**options) as sessions:
                engine=Engine(SQLiteRunStore(state/'jobs.sqlite3'),provider,ROLES,lambda:1000,tool_sessions=sessions)
                engine.accepted_result(job,'work')
                self.assertFalse(engine.step(job))
                self.assertEqual(calls,['generate','audit'])
                grant=sessions.reservations.store.read(engine.status(job)['attempts'][0]['id'])
                self.assertEqual(grant['calls']['write']['state'],'completed')
                self.assertEqual(grant['grant']['remaining'],0)
                retired=engine.retire_workspace(job)
                self.assertEqual(retired['workspace_retirement']['state'],'retired')
                self.assertEqual(retired['workspace_retirement']['proof']['leases']['owned']['state'],'released')
                self.assertEqual(engine.retire_workspace(job),retired)
                contract=sessions.workspace_contracts['owned']()
                identity={key:contract[key] for key in ('device','inode')}
                next_lease=sessions.leases.claim(identity,'next-job',fingerprint(contract))
                replay={**retired,'workspace_retirement':{'state':'retiring'}}
                self.assertEqual(sessions.retire(replay),retired['workspace_retirement']['proof'])
                self.assertTrue(sessions.leases.owns(identity,next_lease))

    def test_prepare_policy_binds_only_host_runtime_without_creating_job_or_lease(self):
        from copy import deepcopy
        from multi_shadow_clone.orchestration.domain.contracts import InvalidContract
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();workspace=root/'workspace';workspace.mkdir()
            with owned_job_sessions(data_dir=root/'state',workspace_id='owned',workspace_root=workspace,paths=['a.js'],lease_path=root/'leases.sqlite3') as sessions:
                provider=ScriptedProvider()
                engine=Engine(SQLiteRunStore(root/'jobs.sqlite3'),provider,ROLES,lambda:1000,tool_sessions=sessions)
                plan=Plan('fixture',(Node('work','R07','change',max_attempts=1),),{})
                draft=dict(version=1,max_calls=1,scopes=[dict(node_id='work',stage='generate',role_id='R07',workspace_id='owned',operations=['apply_changes'],paths=['a.js'],checks=[],max_calls=1,max_bytes=100)])
                original=deepcopy(draft)
                prepared=engine.prepare_execution_policy(plan,draft)
                self.assertEqual(draft,original)
                self.assertEqual(len(prepared['scopes'][0]['runtime_hash']),64)
                self.assertEqual(engine.list_runs(),[])
                self.assertEqual(provider.requests,[])
                contract=sessions.workspace_contracts['owned']()
                self.assertIsNone(sessions.leases.read({key:contract[key] for key in ('device','inode')}))
                with self.assertRaises(InvalidContract):engine.prepare_execution_policy(plan,prepared)
