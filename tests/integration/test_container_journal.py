from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from kagebunshin.execution.domain.admission import Rejected
from kagebunshin.execution.infrastructure.container_journal import ContainerJournal


class ContainerJournalTest(unittest.TestCase):
    def test_supervisor_claim_and_completion_survive_restart_without_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'containers.sqlite3'
            config={'fixture':'guardian'}
            journal=ContainerJournal(path)
            journal.claim('a'*64,config)
            journal.advance('a'*64,config,'reserve_create')
            journal.advance('a'*64,config,'created',container_id='b'*64)
            journal.claim_supervisor('a'*64,config,123)
            restarted=ContainerJournal(path)
            with self.assertRaises(Rejected): restarted.claim_supervisor('a'*64,config,456)
            with self.assertRaises(Rejected): restarted.complete_supervisor('a'*64,config,123,'completed')
            restarted.reserve_attached('a'*64,config,timeout=2,max_output_bytes=100)
            restarted.advance('a'*64,config,'exited',result={'exit_code':137,'oom_killed':False})
            with self.assertRaises(Rejected): restarted.complete_supervisor('a'*64,config,456,'owner_lost')
            result=restarted.complete_supervisor('a'*64,config,123,'owner_lost')
            self.assertEqual(ContainerJournal(path).read('a'*64),result)
            with self.assertRaises(Rejected): restarted.complete_supervisor('a'*64,config,123,'completed')

    def test_restart_and_two_connections_cannot_repeat_uncertain_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'containers.sqlite3'
            config={'endpoint':'unix:///fixture.sock','owner':'fixture','image':'fixed'}
            journal=ContainerJournal(path)
            journal.claim('a'*64,config)
            journal.advance('a'*64,config,'reserve_create')
            restarted=ContainerJournal(path)
            with self.assertRaises(Rejected): restarted.claim('a'*64,config)
            with self.assertRaises(Rejected): restarted.advance('a'*64,config,'reserve_create')
            restarted.advance('a'*64,config,'created',container_id='b'*64)
            gate=Barrier(2)
            def start(_):
                other=ContainerJournal(path)
                gate.wait(timeout=5)
                try:
                    other.advance('a'*64,config,'reserve_start')
                    return 'reserved'
                except Rejected:
                    return 'rejected'
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertCountEqual(list(pool.map(start,range(2))),['reserved','rejected'])
            before=journal.read('a'*64)
            with self.assertRaises(Rejected):
                journal.advance('a'*64,{**config,'endpoint':'unix:///other.sock'},'running')
            self.assertEqual(journal.read('a'*64),before)
            journal.advance('a'*64,config,'reserve_stop')
            result={'exit_code':137,'oom_killed':True}
            journal.advance('a'*64,config,'exited',result=result)
            journal.advance('a'*64,config,'reserve_retire')
            saved=journal.advance('a'*64,config,'absent')
            self.assertEqual(ContainerJournal(path).read('a'*64),saved)
            self.assertEqual(saved['result'],result)
