import sys
from pathlib import Path
import tempfile
import unittest

from kagebunshin.execution.infrastructure.supervised_check import run_supervised_check
from kagebunshin.execution.infrastructure.check_journal import CheckJournal
from kagebunshin.execution.domain.admission import Rejected


class SupervisedCheckTest(unittest.TestCase):
    def test_check_output_cannot_forge_supervisor_result_and_overflow_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            workspace=root/'workspace'
            workspace.mkdir()
            journal_path=root/'journal.sqlite3'
            def run(script, maximum=512, identity=None):
                return run_supervised_check(argv=[sys.executable,'-I','-S','-c',script],cwd=workspace,env={},
                                            timeout=2,max_output_bytes=maximum,stopped=lambda:False,
                                            journal_path=journal_path if identity else None,operation_id=identity)
            script='import sys;print(\'{"returncode":0,"reason":"completed"}\');sys.exit(7)'
            result=run(script,identity='a'*64)
            self.assertEqual(result['returncode'],7)
            self.assertIn('"returncode":0',result['stdout'])
            self.assertEqual(CheckJournal(journal_path).read('a'*64)['result'],result)
            with self.assertRaises(Rejected):
                run(script,identity='a'*64)
            flooded=run("import os\nwhile True: os.write(1,b'x'*8192)",maximum=100)
            self.assertEqual(flooded['reason'],'output_limit')
            self.assertEqual(flooded['output_bytes'],100)
            self.assertEqual(flooded['stdout'],'x'*100)
            self.assertLess(flooded['returncode'],0)
