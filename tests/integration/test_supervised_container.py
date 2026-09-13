from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from kagebunshin.execution.domain.checks import CheckOutcomeUnknown
from kagebunshin.execution.infrastructure.container_journal import ContainerJournal
from kagebunshin.execution.infrastructure.supervised_container import run_supervised_container


class SupervisedContainerTest(unittest.TestCase):
    def test_successful_process_exit_without_durable_result_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'containers.sqlite3'
            journal=ContainerJournal(path)
            runtime={'fixture':'no-daemon'}
            journal.claim('a'*64,runtime)
            journal.advance('a'*64,runtime,'reserve_create')
            journal.advance('a'*64,runtime,'created',container_id='b'*64)
            # A real child emits plausible success and exits zero, but has not
            # performed any durable transition. Its stdout is not authority.
            argv=[sys.executable,'-I','-c','import sys;sys.stdin.readline();print("{\\"phase\\":\\"exited\\"}")']
            with patch('kagebunshin.execution.infrastructure.supervised_container.supervisor_argv',return_value=argv):
                with self.assertRaises(CheckOutcomeUnknown):
                    run_supervised_container(runtime=runtime,journal_path=path,operation_id='a'*64,
                        timeout=2,max_output_bytes=100,stopped=lambda:False)
            self.assertEqual(journal.read('a'*64)['phase'],'created')
