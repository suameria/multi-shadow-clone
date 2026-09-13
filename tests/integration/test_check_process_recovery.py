from hashlib import sha256
from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from multi_shadow_clone.execution.application.executor import Executor
from multi_shadow_clone.execution.application.reservations import Reservations
from multi_shadow_clone.execution.domain.admission import Binding, Grant, Rejected
from multi_shadow_clone.execution.domain.checks import PythonCheck
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.python_checks import PythonChecks
from multi_shadow_clone.execution.infrastructure.sqlite_store import SQLiteOperationStore
from multi_shadow_clone.execution.infrastructure.check_journal import CheckJournal


@unittest.skipUnless(sys.platform == 'darwin','macOS durable check recovery')
class CheckProcessRecoveryTest(unittest.TestCase):
    def test_owner_death_during_check_recovers_and_retires_owned_snapshot(self):
        runtime=Path(sys.base_prefix).resolve()
        executable=runtime/'Resources/Python.app/Contents/MacOS/Python'
        if not executable.is_file():
            self.skipTest('requires explicitly tested framework runtime')
        child='''import sys
from pathlib import Path
from hashlib import sha256
sys.path.insert(0,sys.argv[1])
from multi_shadow_clone.execution.application.executor import Executor
from multi_shadow_clone.execution.application.reservations import Reservations
from multi_shadow_clone.execution.domain.admission import Binding
from multi_shadow_clone.execution.domain.checks import PythonCheck
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.python_checks import PythonChecks
from multi_shadow_clone.execution.infrastructure.sqlite_store import SQLiteOperationStore
root=Path(sys.argv[2]); runtime=Path(sys.argv[3]); executable=Path(sys.argv[4])
definition=PythonCheck('check','check.py',(('check.py',sha256((root/'source/check.py').read_bytes()).hexdigest()),),5,4096,4096)
files=OwnedFiles(root/'source',{'check.py'})
checks=PythonChecks(definitions=[definition],files=files,runtime=runtime,executable=executable,
                    executable_sha256=sha256(executable.read_bytes()).hexdigest(),snapshot_parent=root/'snapshots',journal_path=root/'checks.sqlite3')
app=Reservations(SQLiteOperationStore(root/'operations.sqlite3'),lambda:10)
binding=Binding('g','r','n','a','t','turn',0,'w','c'*64)
Executor(app,{},checkers={'w':checks}).execute(binding,'one')
'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            source,snapshots=root/'source',root/'snapshots'
            source.mkdir();snapshots.mkdir()
            (root/'keep').write_text('other owned fixture')
            content="import os\nfrom pathlib import Path\nPath('.multi-shadow-clone-scratch/ready').write_text(str(os.getpid()))\nwhile True: pass"
            (source/'check.py').write_text(content)
            definition=PythonCheck('check','check.py',(('check.py',sha256(content.encode()).hexdigest()),),5,4096,4096)
            binding=Binding('g','r','n','a','t','turn',0,'w','c'*64)
            app=Reservations(SQLiteOperationStore(root/'operations.sqlite3'),lambda:10)
            app.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'check'}),1,100,4096),
                         check_contracts={'check':definition.fingerprint()})
            app.reserve(binding,[{'call_id':'one','operation':'run_check','arguments':{'check_id':'check'}}])
            files=OwnedFiles(source,{'check.py'})
            checks=PythonChecks(definitions=[definition],files=files,runtime=runtime,executable=executable,
                                executable_sha256=sha256(executable.read_bytes()).hexdigest(),journal_path=root/'checks.sqlite3')
            owner=subprocess.Popen([sys.executable,'-I','-S','-c',child,str(Path(__file__).resolve().parents[2]/'src'),
                                    str(root),str(runtime),str(executable)],env={},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            journal=CheckJournal(root/'checks.sqlite3')
            identity=Executor.operation_id(binding,'one')
            supervisor_pid=None
            try:
                child_pid=None
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    markers=list(snapshots.glob('*/.multi-shadow-clone-scratch/ready'))
                    if markers:
                        try:
                            child_pid=int(markers[0].read_text())
                            break
                        except ValueError:
                            pass
                    time.sleep(.01)
                self.assertIsNotNone(child_pid,'isolated check did not start')
                saved=journal.read(identity)
                supervisor_pid=saved['supervisor_pid']
                self.assertEqual(saved['state'],'running')
                with self.assertRaises(Rejected):
                    Executor(app,{},checkers={'w':checks}).retire_check(binding,'one')
                owner.kill()
                self.assertEqual(owner.wait(timeout=5),-signal.SIGKILL)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    saved=journal.read(identity)
                    if saved['state']=='completed': break
                    time.sleep(.01)
                self.assertEqual(saved['state'],'completed')
                self.assertEqual(saved['result']['reason'],'owner_lost')
                with self.assertRaises(ProcessLookupError): os.kill(child_pid,0)
                self.assertEqual(len(list(snapshots.iterdir())),1)
                restarted=Reservations(SQLiteOperationStore(root/'operations.sqlite3'),lambda:10)
                executor=Executor(restarted,{},checkers={'w':checks})
                with patch.object(checks,'run_operation',side_effect=AssertionError('must not rerun')):
                    result=executor.recover_check(binding,'one')
                    self.assertFalse(result['receipt']['check']['passed'])
                    self.assertEqual(executor.retire_check(binding,'one'),{'state':'removed'})
                    self.assertEqual(executor.retire_check(binding,'one'),{'state':'removed'})
                self.assertEqual(list(snapshots.iterdir()),[])
                self.assertEqual((root/'keep').read_text(),'other owned fixture')
                self.assertEqual(restarted.store.read('g')['grant']['remaining'],0)
            finally:
                if owner.poll() is None: owner.kill()
                owner.wait(timeout=5)
                owner.stdout.close();owner.stderr.close();files.close()
                if supervisor_pid is not None:
                    deadline=time.monotonic()+7
                    while time.monotonic()<deadline:
                        try: os.kill(supervisor_pid,0)
                        except ProcessLookupError: break
                        time.sleep(.02)
                    else: self.fail('supervisor did not retire')

    def test_sigkill_before_operation_save_recovers_without_rerunning_check(self):
        runtime=Path(sys.base_prefix).resolve()
        executable=runtime/'Resources/Python.app/Contents/MacOS/Python'
        if not executable.is_file():
            self.skipTest('requires explicitly tested framework runtime')
        child='''import os,signal,sys
from pathlib import Path
from hashlib import sha256
sys.path.insert(0,sys.argv[1])
from multi_shadow_clone.execution.application.executor import Executor
from multi_shadow_clone.execution.application.reservations import Reservations
from multi_shadow_clone.execution.domain.admission import Binding
from multi_shadow_clone.execution.domain.checks import PythonCheck
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.python_checks import PythonChecks
from multi_shadow_clone.execution.infrastructure.sqlite_store import SQLiteOperationStore
root=Path(sys.argv[2]); runtime=Path(sys.argv[3]); executable=Path(sys.argv[4])
definition=PythonCheck('check','check.py',(('check.py',sha256((root/'source/check.py').read_bytes()).hexdigest()),),3,4096,4096)
files=OwnedFiles(root/'source',{'check.py'})
checks=PythonChecks(definitions=[definition],files=files,runtime=runtime,executable=executable,
                    executable_sha256=sha256(executable.read_bytes()).hexdigest(),snapshot_parent=root/'snapshots',journal_path=root/'checks.sqlite3')
app=Reservations(SQLiteOperationStore(root/'operations.sqlite3'),lambda:10)
binding=Binding('g','r','n','a','t','turn',0,'w','c'*64)
def die_before_save(*args): os.kill(os.getpid(),signal.SIGKILL)
app.finish=die_before_save
Executor(app,{},checkers={'w':checks}).execute(binding,'one')
'''
        for stop in (False,True):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve()
                source,snapshots=root/'source',root/'snapshots'
                source.mkdir(); snapshots.mkdir()
                content='print("verified before crash")'
                (source/'check.py').write_text(content)
                definition=PythonCheck('check','check.py',(('check.py',sha256(content.encode()).hexdigest()),),3,4096,4096)
                binding=Binding('g','r','n','a','t','turn',0,'w','c'*64)
                app=Reservations(SQLiteOperationStore(root/'operations.sqlite3'),lambda:10)
                app.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'check'}),1,100,4096),
                             check_contracts={'check':definition.fingerprint()})
                app.reserve(binding,[{'call_id':'one','operation':'run_check','arguments':{'check_id':'check'}}])
                killed=subprocess.run([sys.executable,'-I','-S','-c',child,str(Path(__file__).resolve().parents[2]/'src'),
                                       str(root),str(runtime),str(executable)],env={},capture_output=True,text=True,timeout=15)
                self.assertEqual(killed.returncode,-signal.SIGKILL,killed.stderr)
                restarted=Reservations(SQLiteOperationStore(root/'operations.sqlite3'),lambda:10)
                self.assertEqual(restarted.store.read('g')['calls']['one']['state'],'unknown')
                self.assertEqual(list(snapshots.iterdir()),[])
                # The current source is gone: recovery must use the historical
                # receipt, not execute or claim that today's source passed.
                (source/'check.py').unlink()
                files=OwnedFiles(source,{'check.py'})
                try:
                    checks=PythonChecks(definitions=[definition],files=files,runtime=runtime,executable=executable,
                                        executable_sha256=sha256(executable.read_bytes()).hexdigest(),journal_path=root/'checks.sqlite3')
                    if stop: restarted.stop('g')
                    executor=Executor(restarted,{},checkers={'w':checks})
                    with patch.object(checks,'run_operation',side_effect=AssertionError('must not run during recovery')):
                        result=executor.recover_check(binding,'one')
                        self.assertEqual(result['state'],'quarantined' if stop else 'completed')
                        self.assertEqual(result['receipt']['check']['process']['stdout'],'verified before crash\n')
                        self.assertEqual(executor.recover_check(binding,'one'),result)
                    with self.assertRaises(Rejected): executor.execute(binding,'one')
                    self.assertEqual(restarted.store.read('g')['grant']['remaining'],0)
                    self.assertEqual(executor.retire_check(binding,'one'),{'state':'path_absent'})
                    self.assertEqual(executor.retire_check(binding,'one'),{'state':'path_absent'})
                finally:
                    files.close()
