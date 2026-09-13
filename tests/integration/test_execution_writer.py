from pathlib import Path
import tempfile
import signal
import subprocess
import sys
import unittest
from multi_shadow_clone.execution.application.executor import Executor
from multi_shadow_clone.execution.application.reservations import Reservations
from multi_shadow_clone.execution.domain.admission import Binding, Grant, Rejected
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.sqlite_store import SQLiteOperationStore


class ExecutionWriterTest(unittest.TestCase):
    def test_sigkill_after_durable_write_recovers_without_another_effect(self):
        from multi_shadow_clone.execution.infrastructure.write_journal import WriteJournal, JournaledWriter
        child = '''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from multi_shadow_clone.execution.application.executor import Executor
from multi_shadow_clone.execution.application.reservations import Reservations
from multi_shadow_clone.execution.domain.admission import Binding
from multi_shadow_clone.execution.infrastructure.sqlite_store import SQLiteOperationStore
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.write_journal import WriteJournal, JournaledWriter
root = Path(sys.argv[2])
reservations = Reservations(SQLiteOperationStore(root/'operations.sqlite3'), lambda:10)
binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
def terminate_before_result(*args):
    os.kill(os.getpid(), signal.SIGKILL)
reservations.finish = terminate_before_result
files = OwnedFiles(root, {'output'})
writer = JournaledWriter(files, WriteJournal(root/'journal.sqlite3'))
Executor(reservations, {}, writers={'w':writer}).execute(binding, 'write')
'''
        for stop in (False, True):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                reservations = Reservations(SQLiteOperationStore(root/'operations.sqlite3'), lambda:10)
                binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
                reservations.register(Grant(binding, frozenset({'apply_changes'}), frozenset({'output'}), frozenset(), 1, 100, 100))
                reservations.reserve(binding, [{'call_id':'write','operation':'apply_changes','arguments':{'path':'output','before_hash':None,'content':'generated'}}])
                process = subprocess.run([sys.executable, '-I', '-c', child,
                                          str(Path(__file__).resolve().parents[2]/'src'), str(root)],
                                         capture_output=True, text=True, timeout=15)
                self.assertEqual(process.returncode, -signal.SIGKILL, process.stderr)
                inode = (root/'output').stat().st_ino
                restarted = Reservations(SQLiteOperationStore(root/'operations.sqlite3'), lambda:10)
                self.assertEqual(restarted.store.read('g')['calls']['write']['state'], 'unknown')
                if stop:
                    restarted.stop('g')
                files = OwnedFiles(root, {'output'})
                try:
                    writer = JournaledWriter(files, WriteJournal(root/'journal.sqlite3'))
                    executor = Executor(restarted, {}, writers={'w':writer})
                    result = executor.recover_write(binding, 'write')
                    self.assertEqual(result['state'], 'quarantined' if stop else 'completed')
                    self.assertEqual(executor.recover_write(binding, 'write'), result)
                    with self.assertRaises(Rejected):
                        executor.execute(binding, 'write')
                    self.assertEqual((root/'output').stat().st_ino, inode)
                    self.assertEqual((root/'output').read_text(), 'generated')
                    self.assertEqual(restarted.store.read('g')['grant']['remaining'], 0)
                    self.assertEqual(list(root.glob('.multi-shadow-clone-*')), [])
                finally:
                    files.close()

    def test_write_before_receipt_failure_stays_unknown_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            db = root/'operations.sqlite3'
            reservations = Reservations(SQLiteOperationStore(db),lambda:10)
            binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
            reservations.register(Grant(binding,frozenset({'apply_changes'}),frozenset({'output'}),frozenset(),1,100,100))
            reservations.reserve(binding,[{'call_id':'write','operation':'apply_changes','arguments':{'path':'output','before_hash':None,'content':'generated'}}])
            writer = OwnedFiles(root,{'output'})
            try:
                disabled = Executor(reservations,{})
                with self.assertRaises(Rejected): disabled.execute(binding,'write')
                self.assertEqual(reservations.store.read('g')['calls']['write']['state'],'reserved')
                def crash(*args): raise OSError('synthetic receipt storage failure')
                reservations.finish = crash
                with self.assertRaises(OSError): Executor(reservations,{},writers={'w':writer}).execute(binding,'write')
                self.assertEqual((root/'output').read_text(),'generated')
                restarted = Reservations(SQLiteOperationStore(db),lambda:10)
                self.assertEqual(restarted.store.read('g')['calls']['write']['state'],'unknown')
                with self.assertRaises(Rejected): Executor(restarted,{},writers={'w':writer}).execute(binding,'write')
                self.assertEqual((root/'output').read_text(),'generated')
                self.assertEqual(restarted.store.read('g')['grant']['remaining'],0)
                inspect = Executor(restarted,{},writers={'w':writer})
                observed = inspect.inspect_unknown_write(binding,'write')
                self.assertTrue(observed['latest_observation']['desired_state_matches'])
                self.assertFalse(observed['latest_observation']['operation_attribution_proven'])
                self.assertEqual(observed['state'],'unknown')
                (root/'output').write_text('third party')
                observed = inspect.inspect_unknown_write(binding,'write')
                self.assertFalse(observed['latest_observation']['desired_state_matches'])
                self.assertEqual((root/'output').read_text(),'third party')
                self.assertEqual(restarted.store.read('g')['grant']['remaining'],0)
            finally: writer.close()

    def test_durable_write_receipt_recovers_without_rewrite_and_respects_stop(self):
        from multi_shadow_clone.execution.infrastructure.write_journal import WriteJournal, JournaledWriter
        for stop in (False,True):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                db = root/'operations.sqlite3'
                reservations = Reservations(SQLiteOperationStore(db),lambda:10)
                binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
                reservations.register(Grant(binding,frozenset({'apply_changes'}),frozenset({'output'}),frozenset(),1,100,100))
                reservations.reserve(binding,[{'call_id':'write','operation':'apply_changes','arguments':{'path':'output','before_hash':None,'content':'generated'}}])
                files = OwnedFiles(root,{'output'})
                writer = JournaledWriter(files,WriteJournal(root/'journal.sqlite3'))
                try:
                    def crash(*args): raise OSError('synthetic result save failure')
                    reservations.finish = crash
                    with self.assertRaises(OSError): Executor(reservations,{},writers={'w':writer}).execute(binding,'write')
                    inode = (root/'output').stat().st_ino
                    restarted = Reservations(SQLiteOperationStore(db),lambda:10)
                    if stop: restarted.stop('g')
                    executor = Executor(restarted,{},writers={'w':JournaledWriter(files,WriteJournal(root/'journal.sqlite3'))})
                    result = executor.recover_write(binding,'write')
                    self.assertEqual(result['state'],'quarantined' if stop else 'completed')
                    self.assertEqual(executor.recover_write(binding,'write'),result)
                    self.assertEqual((root/'output').stat().st_ino,inode)
                    self.assertEqual(restarted.store.read('g')['grant']['remaining'],0)
                finally: files.close()
