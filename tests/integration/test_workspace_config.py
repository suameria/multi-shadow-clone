from pathlib import Path
import json,tempfile,unittest
from multi_shadow_clone.execution.presentation.workspace_config import load_workspace_config
from multi_shadow_clone.execution.domain.admission import Rejected

class WorkspaceConfigTest(unittest.TestCase):
    def test_explicit_configuration_rejects_extra_authority_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            valid={'workspace_id':'owned','workspace_root':directory,'paths':['src/a.js']}
            path.write_text(json.dumps(valid));self.assertEqual(load_workspace_config(path),valid)
            for change in ({'shell':'anything'},{'paths':['../a']},{'paths':['a','a']},{'workspace_root':'relative'}):
                path.write_text(json.dumps({**valid,**change}))
                with self.assertRaises(Rejected):load_workspace_config(path)
            path.write_text('{"workspace_id":"one","workspace_id":"two"}')
            with self.assertRaises(Rejected):load_workspace_config(path)

    def test_operator_engine_scope_reopens_explicit_jobs_and_closes_workspace(self):
        from multi_shadow_clone.bootstrap import engine_scope
        from multi_shadow_clone.orchestration.domain.contracts import Node, Plan
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            workspace=root/'workspace';workspace.mkdir()
            (workspace/'a.js').write_text('fixture')
            config=root/'workspace.json'
            config.write_text(json.dumps(dict(workspace_id='owned',workspace_root=str(workspace),paths=['a.js'])))
            with engine_scope('offline',data_dir=root/'state',workspace_config=config) as engine:
                plan=Plan('fixture',(Node('work','R07','read',audit_role='R12',max_attempts=1),),{})
                policy=engine.prepare_execution_policy(plan,dict(version=1,max_calls=1,scopes=[dict(
                    node_id='work',stage='generate',role_id='R07',workspace_id='owned',operations=['read_files'],
                    paths=['a.js'],checks=[],max_calls=1,max_bytes=100)]))
                job=engine.create(plan,execution_policy=policy)
                sessions=engine.tool_sessions
            with self.assertRaises(Exception):sessions.workspace_contracts['owned']()
            with engine_scope('offline',data_dir=root/'state',workspace_config=config) as engine:
                self.assertTrue(engine.execution_status(job)['compatible'])
                self.assertEqual(engine.status(job)['attempts'],[])
            with engine_scope('offline',data_dir=root/'state') as engine:
                self.assertFalse(engine.execution_status(job)['compatible'])
