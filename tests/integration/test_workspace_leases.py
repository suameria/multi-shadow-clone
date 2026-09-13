from pathlib import Path
import tempfile
import unittest
from multi_shadow_clone.execution.infrastructure.workspace_leases import WorkspaceLeases
from multi_shadow_clone.execution.domain.admission import Rejected

class WorkspaceLeasesTest(unittest.TestCase):
    def test_restart_preserves_owner_and_stale_release_cannot_affect_next_job(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'leases.sqlite3';identity={'device':1,'inode':2}
            first=WorkspaceLeases(path).claim(identity,'one','a'*64)
            store=WorkspaceLeases(path)
            self.assertEqual(store.claim(identity,'one','a'*64),first)
            with self.assertRaises(Rejected):store.claim(identity,'two','a'*64)
            with self.assertRaises(Rejected):store.claim(identity,'one','b'*64)
            released=store.release(identity,first,'c'*64)
            self.assertEqual(store.release(identity,first,'c'*64),released)
            second=store.claim(identity,'two','a'*64)
            self.assertEqual(second['generation'],2)
            with self.assertRaises(Rejected):store.release(identity,first,'c'*64)
            self.assertEqual(store.claim(identity,'two','a'*64),second)

    def test_alias_names_cannot_change_physical_identity_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store=WorkspaceLeases(Path(directory)/'leases.sqlite3')
            identity={'inode':8,'device':4}
            store.claim(identity,'one','a'*64)
            with self.assertRaises(Rejected):store.claim({'device':4,'inode':8},'two','b'*64)
            with self.assertRaises(Rejected):store.claim({'device':True,'inode':8},'two','b'*64)

    def test_two_workers_cannot_claim_the_same_workspace(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'leases.sqlite3'
            stores=[WorkspaceLeases(path),WorkspaceLeases(path)];barrier=Barrier(2)
            def claim(index):
                barrier.wait()
                try:return stores[index].claim({'device':1,'inode':2},str(index),'a'*64)['run_id']
                except Rejected:return None
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,(0,1)))
            self.assertEqual(sum(value is not None for value in results),1)
