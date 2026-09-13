from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from kagebunshin.execution.application.executor import Executor
from kagebunshin.execution.application.reservations import Reservations
from kagebunshin.execution.domain.admission import Binding, Grant, Rejected
from kagebunshin.execution.infrastructure.owned_files import OwnedFiles
from kagebunshin.execution.infrastructure.sqlite_store import SQLiteOperationStore


class ExecutionReaderTest(unittest.TestCase):
    def test_reserved_file_read_is_saved_and_never_implicitly_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'input.txt').write_text('sample')
            binding = Binding('g','r','n','a','t','turn',0,'workspace','c'*64)
            reservations = Reservations(SQLiteOperationStore(root/'state.sqlite3'),lambda:10)
            reservations.register(Grant(binding,frozenset({'read_files'}),frozenset({'input.txt'}),frozenset(),2,100,100))
            proposal = {'call_id':'read','operation':'read_files','arguments':{'files':[{'path':'input.txt','expected_hash':sha256(b'sample').hexdigest()}],'max_bytes':100}}
            reservations.reserve(binding,[proposal])
            reader = OwnedFiles(root, {'input.txt'})
            try:
                executor = Executor(reservations,{'workspace':reader})
                result = executor.execute(binding,'read')
                self.assertEqual(result['state'],'completed')
                self.assertEqual(result['receipt']['files'][0]['content'],'sample')
                (root/'input.txt').write_text('changed')
                with self.assertRaises(Rejected): executor.execute(binding,'read')
                stored = SQLiteOperationStore(root/'state.sqlite3').read('g')
                self.assertEqual(stored['calls']['read']['receipt']['files'][0]['content'],'sample')
                self.assertEqual(stored['grant']['remaining'],1)
            finally: reader.close()

    def test_stop_before_read_has_no_adapter_call_and_late_read_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = Binding('g','r','n','a','t','turn',0,'workspace','c'*64)
            reservations = Reservations(SQLiteOperationStore(root/'state.sqlite3'),lambda:10)
            reservations.register(Grant(binding,frozenset({'read_files'}),frozenset({'input'}),frozenset(),2,100,100))
            proposal = {'call_id':'read','operation':'read_files','arguments':{'files':[{'path':'input','expected_hash':'a'*64}],'max_bytes':100}}
            reservations.reserve(binding,[proposal])
            reservations.reserve(binding,[dict(proposal, call_id='pending')])
            class Reader:
                calls = 0
                def read_files(self, **kwargs):
                    self.calls += 1
                    reservations.stop('g')
                    return [{'content':'late'}]
            reader = Reader()
            executor = Executor(reservations,{'workspace':reader})
            result = executor.execute(binding,'read')
            self.assertEqual(result['state'],'quarantined')
            with self.assertRaises(Rejected): executor.execute(binding,'read')
            with self.assertRaises(Rejected): executor.execute(binding,'pending')
            self.assertEqual(reader.calls,1)
            self.assertEqual(reservations.store.read('g')['calls']['pending']['state'],'cancelled')
            self.assertEqual(reservations.store.read('g')['grant']['remaining'],1)
