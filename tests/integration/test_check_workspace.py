from hashlib import sha256
from pathlib import Path
import tempfile
import shutil
import unittest

from multi_shadow_clone.execution.domain.admission import Rejected
from multi_shadow_clone.execution.domain.checks import CheckOutcomeUnknown
from multi_shadow_clone.execution.infrastructure.check_workspace import check_workspace
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles


class CheckWorkspaceTest(unittest.TestCase):
    def test_unknown_process_retains_workspace_for_owned_recovery(self):
        data='print("check")'
        observation={'path':'check.py','content':data,'sha256':sha256(data.encode()).hexdigest(),'bytes':len(data)}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CheckOutcomeUnknown) as raised:
                with check_workspace([observation],max_bytes=100,parent=directory):
                    raise CheckOutcomeUnknown('synthetic uncertain supervisor')
            retained=Path(raised.exception.workspace_path)
            self.assertTrue((retained/'check.py').is_file())
            # No process was launched in this fixture, so its owner can retire it.
            shutil.rmtree(retained)
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_snapshot_contains_only_declared_bytes_and_is_removed_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            source = parent/'source'
            source.mkdir()
            (source/'check.py').write_text('print("ok")')
            (source/'private').write_text('canary-not-an-input')
            files = OwnedFiles(source, {'check.py'})
            try:
                observations = files.read_files([{'path':'check.py','expected_hash':sha256(b'print("ok")').hexdigest()}],100)
            finally:
                files.close()
            snapshot_path = None
            with self.assertRaisesRegex(RuntimeError, 'simulated check failure'):
                with check_workspace(observations,max_bytes=100,parent=parent) as snapshot:
                    snapshot_path = snapshot.root
                    self.assertEqual((snapshot.root/'check.py').read_bytes(), b'print("ok")')
                    self.assertFalse((snapshot.root/'private').exists())
                    self.assertEqual((snapshot.root/'check.py').stat().st_nlink,1)
                    self.assertNotEqual((snapshot.root/'check.py').stat().st_ino,(source/'check.py').stat().st_ino)
                    self.assertEqual(list(snapshot.scratch.iterdir()),[])
                    (source/'check.py').write_text('changed after observation')
                    self.assertEqual((snapshot.root/'check.py').read_bytes(), b'print("ok")')
                    # Cleanup must unlink this link, never delete the target.
                    (snapshot.scratch/'escape').symlink_to(source, target_is_directory=True)
                    raise RuntimeError('simulated check failure')
            self.assertFalse(snapshot_path.exists())
            self.assertEqual((source/'private').read_text(),'canary-not-an-input')
            self.assertEqual(list(parent.iterdir()),[source])

    def test_bad_observations_leave_no_partial_snapshot(self):
        def observation(path='check.py',content='ok'):
            data=content.encode()
            return {'path':path,'content':content,'sha256':sha256(data).hexdigest(),'bytes':len(data)}
        cases = [
            [dict(observation(),sha256='0'*64)],
            [observation(),observation()],
            [observation('folder'),observation('folder/child')],
            [observation('.multi-shadow-clone-scratch/data')],
            [observation('../outside')],
            [observation(content='too many bytes')],
        ]
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory).resolve()
            for inputs in cases:
                with self.subTest(inputs=inputs), self.assertRaises(Rejected):
                    with check_workspace(inputs,max_bytes=10,parent=parent):
                        self.fail('invalid snapshot admitted')
                self.assertEqual(list(parent.iterdir()),[])
