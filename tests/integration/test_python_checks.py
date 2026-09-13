from hashlib import sha256
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from multi_shadow_clone.execution.domain.admission import Rejected, Binding, Grant
from multi_shadow_clone.execution.application.executor import Executor
from multi_shadow_clone.execution.application.reservations import Reservations
from multi_shadow_clone.execution.infrastructure.sqlite_store import SQLiteOperationStore
from multi_shadow_clone.execution.domain.checks import PythonCheck
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.python_checks import PythonChecks


@unittest.skipUnless(sys.platform == 'darwin', 'macOS registered check integration')
class PythonChecksTest(unittest.TestCase):
    def test_stop_during_real_check_kills_child_and_quarantines_receipt(self):
        runtime = Path(sys.base_prefix).resolve()
        executable = runtime/'Resources/Python.app/Contents/MacOS/Python'
        if not executable.is_file():
            self.skipTest('requires explicitly tested framework runtime')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, snapshots = root/'source', root/'snapshots'
            source.mkdir()
            snapshots.mkdir()
            content = "from pathlib import Path\nPath('.multi-shadow-clone-scratch/ready').write_text('ready')\nwhile True: pass"
            (source/'check.py').write_text(content)
            definition = PythonCheck('check','check.py',(('check.py',sha256(content.encode()).hexdigest()),),5,4096,4096)
            files = OwnedFiles(source,{'check.py'})
            try:
                checks = PythonChecks(definitions=[definition],files=files,runtime=runtime,executable=executable,
                                      executable_sha256=sha256(executable.read_bytes()).hexdigest(),snapshot_parent=snapshots,journal_path=root/'check-journal.sqlite3')
                db = root/'operations.sqlite3'
                app = Reservations(SQLiteOperationStore(db),lambda:10)
                stopper = Reservations(SQLiteOperationStore(db),lambda:10)
                binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
                app.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'check'}),1,100,4096),
                             check_contracts={'check':checks.contract_hash('check')})
                app.reserve(binding,[{'call_id':'one','operation':'run_check','arguments':{'check_id':'check'}}])
                def stop_after_process_marker():
                    deadline = time.monotonic()+4
                    while time.monotonic() < deadline:
                        if list(snapshots.glob('*/.multi-shadow-clone-scratch/ready')):
                            stopper.stop('g')
                            return
                        time.sleep(.01)
                    raise AssertionError('isolated child did not start')
                with ThreadPoolExecutor(max_workers=1) as pool:
                    stopped = pool.submit(stop_after_process_marker)
                    result = Executor(app,{},checkers={'w':checks}).execute(binding,'one')
                    stopped.result()
                self.assertEqual(result['state'],'quarantined')
                self.assertFalse(result['receipt']['check']['passed'])
                self.assertEqual(result['receipt']['check']['process']['reason'],'stopped')
                self.assertLess(result['receipt']['check']['process']['returncode'],0)
                self.assertEqual(app.store.read('g')['grant']['remaining'],0)
                self.assertEqual(list(snapshots.iterdir()),[])
            finally:
                files.close()

    def test_registered_snapshot_check_pass_failure_and_changed_input(self):
        runtime = Path(sys.base_prefix).resolve()
        executable = runtime/'Resources/Python.app/Contents/MacOS/Python'
        if not executable.is_file():
            self.skipTest('requires explicitly tested framework runtime')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root/'source'
            source.mkdir()
            snapshots = root/'snapshots'
            snapshots.mkdir()
            contents = {'helper.py':'answer = 42',
                        'check.py':'import helper\nassert helper.answer == 42\nprint("checked")',
                        'fail.py':'raise AssertionError("intentional check failure")'}
            for path, content in contents.items():
                (source/path).write_text(content)
            digest = lambda path: sha256(contents[path].encode()).hexdigest()
            definitions = [PythonCheck('pass','check.py',(('check.py',digest('check.py')),('helper.py',digest('helper.py'))),2,4096,4096),
                           PythonCheck('fail','fail.py',(('fail.py',digest('fail.py')),),2,4096,4096)]
            files = OwnedFiles(source,set(contents))
            try:
                checks = PythonChecks(definitions=definitions, files=files, runtime=runtime,
                                      executable=executable, executable_sha256=sha256(executable.read_bytes()).hexdigest(),
                                      snapshot_parent=snapshots,journal_path=root/'check-journal.sqlite3')
                passed = checks.run_check('pass',max_bytes=4096,timeout=3,stopped=lambda:False)
                self.assertTrue(passed['passed'])
                self.assertEqual(passed['definition_hash'],definitions[0].fingerprint())
                self.assertEqual(passed['process']['stdout'],'checked\n')
                self.assertEqual({item['path'] for item in passed['inputs']},{'check.py','helper.py'})
                failed = checks.run_check('fail',max_bytes=4096,timeout=3,stopped=lambda:False)
                self.assertFalse(failed['passed'])
                self.assertIn('intentional check failure',failed['process']['stderr'])
                binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
                db = root/'operations.sqlite3'
                reservations = Reservations(SQLiteOperationStore(db),lambda:10)
                reservations.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'pass'}),1,100,4096),
                                      check_contracts={'pass':checks.contract_hash('pass')})
                reservations.reserve(binding,[{'call_id':'check','operation':'run_check','arguments':{'check_id':'pass'}}])
                restarted = Reservations(SQLiteOperationStore(db),lambda:10)
                changed = PythonChecks(definitions=[replace(definitions[0],timeout=3)], files=files,
                                       runtime=runtime, executable=executable,
                                       executable_sha256=sha256(executable.read_bytes()).hexdigest(),snapshot_parent=snapshots,journal_path=root/'check-journal.sqlite3')
                with self.assertRaises(Rejected):
                    Executor(restarted,{},checkers={'w':changed}).execute(binding,'check')
                self.assertEqual(restarted.store.read('g')['calls']['check']['state'],'reserved')
                self.assertEqual(list(snapshots.iterdir()),[])
                executor = Executor(restarted,{},checkers={'w':checks})
                executed = executor.execute(binding,'check')
                self.assertEqual(executed['state'],'completed')
                self.assertTrue(executed['receipt']['check']['passed'])
                self.assertEqual(executed['started_check_contract'],checks.contract_hash('pass'))
                with self.assertRaises(Rejected):
                    executor.execute(binding,'check')
                self.assertEqual(restarted.store.read('g')['grant']['remaining'],0)
                for stop in (False,True):
                    with self.subTest(recovery_stop=stop):
                        recovery_binding=replace(binding,grant_id='recovery'+str(stop),run_id='recovery'+str(stop))
                        recovery=Reservations(SQLiteOperationStore(db),lambda:10)
                        recovery.register(Grant(recovery_binding,frozenset({'run_check'}),frozenset(),frozenset({'pass'}),1,100,4096),
                                          check_contracts={'pass':checks.contract_hash('pass')})
                        recovery.reserve(recovery_binding,[{'call_id':'check','operation':'run_check','arguments':{'check_id':'pass'}}])
                        with patch.object(recovery,'finish',side_effect=OSError('synthetic operation save failure')):
                            with self.assertRaises(OSError):
                                Executor(recovery,{},checkers={'w':checks}).execute(recovery_binding,'check')
                        resumed=Reservations(SQLiteOperationStore(db),lambda:10)
                        self.assertEqual(resumed.store.read(recovery_binding.grant_id)['calls']['check']['state'],'unknown')
                        if stop:
                            resumed.stop(recovery_binding.grant_id)
                        resume_executor=Executor(resumed,{},checkers={'w':checks})
                        with patch.object(checks,'run_operation',side_effect=AssertionError('recovery must not rerun')):
                            restored=resume_executor.recover_check(recovery_binding,'check')
                            self.assertEqual(restored['state'],'quarantined' if stop else 'completed')
                            self.assertTrue(restored['receipt']['check']['passed'])
                            self.assertEqual(resume_executor.recover_check(recovery_binding,'check'),restored)
                        self.assertEqual(resumed.store.read(recovery_binding.grant_id)['grant']['remaining'],0)
                        self.assertEqual(list(snapshots.iterdir()),[])
                with self.assertRaises(Rejected):
                    checks.run_check('unregistered',max_bytes=4096,timeout=3,stopped=lambda:False)
                (source/'helper.py').write_text('answer = 0')
                with self.assertRaises(Rejected):
                    checks.run_check('pass',max_bytes=4096,timeout=3,stopped=lambda:False)
                self.assertEqual(list(snapshots.iterdir()),[])
                self.assertEqual((source/'check.py').read_text(),contents['check.py'])
            finally:
                files.close()
