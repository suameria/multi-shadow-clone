from hashlib import sha256
from pathlib import Path
import shutil
import tempfile
import unittest

from multi_shadow_clone.execution.domain.admission import Rejected
from multi_shadow_clone.execution.domain.checks import CheckOutcomeUnknown
from multi_shadow_clone.execution.infrastructure.check_workspace import check_workspace
from multi_shadow_clone.execution.infrastructure.check_retirement import retire_check_workspace


class CheckRetirementTest(unittest.TestCase):
    def retained(self,parent):
        observation={'path':'check.py','content':'ok','sha256':sha256(b'ok').hexdigest(),'bytes':2}
        with self.assertRaises(CheckOutcomeUnknown):
            with check_workspace([observation],max_bytes=10,parent=parent) as snapshot:
                with self.assertRaises(Rejected):
                    retire_check_workspace(snapshot.ownership)
                raise CheckOutcomeUnknown('synthetic stopped owner')
        return snapshot

    def test_retire_owned_snapshot_without_following_scratch_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            outside=root/'outside'
            outside.mkdir()
            (outside/'keep').write_text('untouched')
            snapshot=self.retained(root)
            (snapshot.scratch/'escape').symlink_to(outside,target_is_directory=True)
            self.assertEqual(retire_check_workspace(snapshot.ownership),{'state':'removed'})
            self.assertEqual(retire_check_workspace(snapshot.ownership),{'state':'path_absent'})
            self.assertEqual((outside/'keep').read_text(),'untouched')

    def test_same_path_replacement_and_changed_marker_are_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            snapshot=self.retained(root)
            moved=root/'moved-owned-fixture'
            snapshot.root.rename(moved)
            snapshot.root.mkdir()
            (snapshot.root/'.multi-shadow-clone-owner').write_bytes((moved/'.multi-shadow-clone-owner').read_bytes())
            (snapshot.root/'keep').write_text('replacement')
            with self.assertRaises(Rejected): retire_check_workspace(snapshot.ownership)
            self.assertEqual((snapshot.root/'keep').read_text(),'replacement')
            shutil.rmtree(snapshot.root)
            moved.rename(snapshot.root)
            marker=snapshot.root/'.multi-shadow-clone-owner'
            original=marker.read_bytes()
            marker.chmod(0o600)
            marker.write_text('changed')
            with self.assertRaises(Rejected): retire_check_workspace(snapshot.ownership)
            self.assertTrue(snapshot.root.exists())
            marker.write_bytes(original)
            self.assertEqual(retire_check_workspace(snapshot.ownership),{'state':'removed'})
