from pathlib import Path
import json
from hashlib import sha256
import tempfile
import unittest
from multi_shadow_clone.execution.domain.admission import Rejected
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.infrastructure.write_journal import WriteJournal,JournaledWriter


class WriteJournalTest(unittest.TestCase):
    def test_completed_receipt_survives_restart_and_pending_never_reexecutes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            files = OwnedFiles(root,{'output'})
            path = root/'journal.sqlite3'
            journal = WriteJournal(path)
            try:
                arguments = {'path':'output','before_hash':None,'content':'result'}
                fingerprint = sha256(json.dumps({'operation':'apply_changes','arguments':arguments},sort_keys=True,separators=(',',':')).encode()).hexdigest()
                receipt = JournaledWriter(files,journal).apply_operation('a'*64,fingerprint,arguments,100)
                restarted = WriteJournal(path)
                self.assertEqual(restarted.read('a'*64,fingerprint),{'state':'completed','receipt':receipt})
                with self.assertRaises(Rejected): restarted.claim('a'*64,fingerprint)
                with self.assertRaises(Rejected): restarted.complete('a'*64,fingerprint,{'different':True})
                restarted.claim('c'*64,'d'*64)
                self.assertEqual(WriteJournal(path).read('c'*64,'d'*64),{'state':'unknown'})
                with self.assertRaises(Rejected): restarted.claim('c'*64,'d'*64)
                self.assertEqual((root/'output').read_text(),'result')
            finally: files.close()
