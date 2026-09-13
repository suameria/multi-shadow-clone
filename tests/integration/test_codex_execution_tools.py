from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from multi_shadow_clone.execution.application.executor import Executor
from multi_shadow_clone.execution.application.reservations import Reservations
from multi_shadow_clone.execution.domain.admission import Binding, Grant, Rejected
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.sqlite_store import SQLiteOperationStore
from multi_shadow_clone.execution.presentation.codex_tools import CodexTools, PreparedCodexTools


class CodexExecutionToolsTest(unittest.TestCase):
    def test_prepared_grant_rejects_wrong_attempt_and_binds_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            reservations=Reservations(SQLiteOperationStore(Path(directory)/'state.sqlite3'),lambda:10)
            binding=Binding('g','run','node','attempt','pending','pending',0,'workspace','c'*64)
            grant=Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'registered'}),1,100,100)
            prepared=PreparedCodexTools(grant,reservations,Executor(reservations,{}),check_contracts={'registered':'a'*64})
            with self.assertRaises(Rejected): prepared.validate_request('other','run','node','c'*64)
            with self.assertRaises(Rejected): prepared.handle({})
            prepared.validate_request('attempt','run','node','c'*64)
            prepared.bind('actual_thread','actual_turn')
            stored=reservations.store.read('g')['grant']['binding']
            self.assertEqual((stored['thread_id'],stored['turn_id']),('actual_thread','actual_turn'))
            with self.assertRaises(Rejected): prepared.bind('replacement','replacement')

    def test_foreign_call_is_rejected_and_replay_returns_saved_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'input.txt').write_text('sample')
            binding=Binding('g','r','n','a','thread','turn',0,'workspace','c'*64)
            reservations=Reservations(SQLiteOperationStore(root/'state.sqlite3'),lambda:10)
            reservations.register(Grant(binding,frozenset({'read_files'}),frozenset({'input.txt'}),frozenset(),1,100,100))
            reader=OwnedFiles(root,{'input.txt'})
            try:
                gateway=CodexTools(binding,reservations,Executor(reservations,{'workspace':reader}))
                call={'threadId':'thread','turnId':'turn','callId':'call_1','tool':'multi_shadow_clone_read_files',
                    'arguments':{'files':[{'path':'input.txt','expected_hash':sha256(b'sample').hexdigest()}],'max_bytes':100}}
                for changed in ({**call,'threadId':'foreign'},{**call,'turnId':'old'},
                                {**call,'namespace':'shell'},{**call,'tool':'exec'},{**call,'tool':[]}):
                    with self.assertRaises(Rejected): gateway.handle(changed)
                self.assertEqual(reservations.store.read('g')['grant']['remaining'],1)
                result=gateway.handle(call)
                self.assertTrue(result['success'])
                self.assertEqual(json.loads(result['contentItems'][0]['text'])['receipt']['files'][0]['content'],'sample')
                (root/'input.txt').unlink()
                self.assertEqual(gateway.handle(call),result)
                self.assertEqual(reservations.store.read('g')['grant']['remaining'],0)
                reservations.stop('g')
                with self.assertRaises(Rejected): gateway.handle(call)
            finally: reader.close()

    def test_host_stop_during_effect_is_durable_and_result_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SQLiteOperationStore(Path(directory)/'state.sqlite3')
            reservations=Reservations(store,lambda:10)
            binding=Binding('g','r','n','a','thread','turn',0,'workspace','c'*64)
            reservations.register(Grant(binding,frozenset({'read_files'}),frozenset({'input.txt'}),frozenset(),1,100,100))
            allowed=[True]
            class Reader:
                def read_files(self,**kwargs):
                    allowed[0]=False
                    return [{'content':'observed before stop'}]
            gateway=CodexTools(binding,reservations,Executor(reservations,{'workspace':Reader()}),may_continue=lambda:allowed[0])
            call={'threadId':'thread','turnId':'turn','callId':'call','tool':'multi_shadow_clone_read_files',
                  'arguments':{'files':[{'path':'input.txt','expected_hash':'a'*64}],'max_bytes':100}}
            result=gateway.handle(call)
            self.assertFalse(result['success'])
            self.assertEqual(json.loads(result['contentItems'][0]['text']),{'state':'quarantined'})
            saved=SQLiteOperationStore(Path(directory)/'state.sqlite3').read('g')
            self.assertTrue(saved['stopped'])
            self.assertEqual(saved['grant']['remaining'],0)
            self.assertEqual(saved['calls']['call']['state'],'quarantined')
            with self.assertRaises(Rejected):gateway.handle(call)

    def test_unavailable_host_guard_stops_before_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            reservations=Reservations(SQLiteOperationStore(Path(directory)/'state.sqlite3'),lambda:10)
            binding=Binding('g','r','n','a','thread','turn',0,'workspace','c'*64)
            reservations.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'test'}),1,100,100))
            def unavailable():raise OSError('job unavailable')
            gateway=CodexTools(binding,reservations,Executor(reservations,{}),may_continue=unavailable)
            with self.assertRaises(Rejected):
                gateway.handle({'threadId':'thread','turnId':'turn','callId':'call','tool':'multi_shadow_clone_run_check','arguments':{'check_id':'test'}})
            saved=reservations.store.read('g')
            self.assertTrue(saved['stopped'])
            self.assertEqual(saved['calls'],{})
            self.assertEqual(saved['grant']['remaining'],1)
