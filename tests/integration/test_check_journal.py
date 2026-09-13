from pathlib import Path
import tempfile
import unittest

from kagebunshin.execution.domain.admission import Rejected
from kagebunshin.execution.infrastructure.check_journal import CheckJournal


class CheckJournalTest(unittest.TestCase):
    def test_restart_retains_exact_config_and_immutable_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'checks.sqlite3'
            journal = CheckJournal(path)
            config = {'argv':['/host/check'],'timeout':2}
            journal.claim('a'*64,config)
            with self.assertRaises(Rejected):
                journal.claim('a'*64,config)
            with self.assertRaises(Rejected):
                journal.start('a'*64,{**config,'timeout':3},123)
            journal.start('a'*64,config,123)
            restarted = CheckJournal(path)
            self.assertEqual(restarted.read('a'*64)['state'],'running')
            with self.assertRaises(Rejected):
                restarted.start('a'*64,config,456)
            result={'reason':'completed','returncode':1}
            saved=restarted.complete('a'*64,config,result)
            self.assertEqual(CheckJournal(path).read('a'*64),saved)
            self.assertEqual(restarted.complete('a'*64,config,result),saved)
            with self.assertRaises(Rejected):
                restarted.complete('a'*64,config,{'reason':'completed','returncode':0})
