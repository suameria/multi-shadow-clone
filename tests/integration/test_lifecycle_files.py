from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kagebunshin.delivery.domain.model import InvalidDelivery
from kagebunshin.delivery.infrastructure.lifecycle_files import CommandJournal, LifecycleFiles


class LifecycleFilesTest(unittest.TestCase):
    def test_real_process_receipt_and_output_survive_restart_and_reject_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            files = LifecycleFiles(Path(directory))
            state = {"id": "environment-" + "b" * 32, "phase": "creating"}
            operation_id, cwd = "a" * 32, Path(directory)
            args = [sys.executable, "-I", "-c", "print('owned fixture')"]
            with files.locked():
                files.save(state)
                CommandJournal(files).execute(state, "test-fixture", operation_id, args, cwd)
            restarted = LifecycleFiles(Path(directory))
            self.assertEqual(restarted.read(), state)
            receipt = CommandJournal(restarted).read(state, "test-fixture", operation_id, args, cwd)
            self.assertEqual(receipt["returncode"], 0)
            self.assertEqual(receipt["output"], "owned fixture\n")
            with self.assertRaises(InvalidDelivery):
                CommandJournal(restarted).execute(state, "test-fixture", operation_id, args, cwd)
            (cwd / ("command-" + operation_id + ".log")).write_text("modified")
            with self.assertRaises(InvalidDelivery):
                CommandJournal(restarted).read(state, "test-fixture", operation_id, args, cwd)

    def test_interrupted_process_keeps_partial_log_without_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            files, operation_id = LifecycleFiles(Path(directory)), "a" * 32
            state = {"id": "environment-" + "b" * 32}
            args = [sys.executable, "-I", "-c", "import time; print('before interrupt', flush=True); time.sleep(10)"]
            with files.locked():
                with self.assertRaises(subprocess.TimeoutExpired):
                    CommandJournal(files).execute(state, "fixture", operation_id, args, Path(directory), timeout=0.4)
            self.assertIn("before interrupt", (Path(directory) / ("command-" + operation_id + ".log")).read_text())
            self.assertIsNone(CommandJournal(files).read(state, "fixture", operation_id, args, Path(directory)))

    def test_lock_excludes_another_owner_and_old_retired_identity_is_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            one, two = LifecycleFiles(Path(directory)), LifecycleFiles(Path(directory))
            with one.locked():
                with self.assertRaises(BlockingIOError):
                    with two.locked(): self.fail("second owner entered")
            old = {"id": "environment-" + "b" * 32, "phase": "retired"}
            one.save(old)
            one.save({"id": "environment-" + "c" * 32, "phase": "creating"})
            self.assertEqual(json.loads((Path(directory) / (old["id"] + ".json")).read_text()), old)
            with self.assertRaises(InvalidDelivery): one.save(old)

    def test_corrupt_identity_cannot_choose_an_archive_path(self):
        with tempfile.TemporaryDirectory() as directory:
            files = LifecycleFiles(Path(directory))
            bad = {"id": "../escaped", "phase": "retired"}
            with self.assertRaises(InvalidDelivery): files.save(bad)
            path = Path(directory) / "lifecycle.json"
            path.touch(mode=0o600)
            path.write_text(json.dumps(bad))
            with self.assertRaises(InvalidDelivery):
                files.save({"id": "environment-" + "b" * 32, "phase": "creating"})
            self.assertEqual(json.loads(path.read_text()), bad)
