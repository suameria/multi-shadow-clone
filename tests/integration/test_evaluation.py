from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import sys
from threading import Barrier
import unittest

from kagebunshin.evaluation.application.batch import Batch
from kagebunshin.evaluation.infrastructure.sqlite_store import SQLiteStudyStore
from kagebunshin.evaluation.infrastructure.verification import VerifiedStudy


class StudySQLiteTest(unittest.TestCase):
    def test_actual_sqlite_reservation_has_one_owner_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "study.sqlite3"
            store = SQLiteStudyStore(path)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: store.reserve("case:arm", {"state": "preparing"}), range(2)))
            self.assertEqual(sorted(results), [False, True])
            SQLiteStudyStore(path).save("case:arm", {"state": "failed"})
            self.assertEqual(store.list_records(), [{"state": "failed"}])
            self.assertEqual(store.read("case:arm"), {"state": "failed"})


class StudyReceiptTest(unittest.TestCase):
    def test_same_build_cannot_mix_two_suites_in_one_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); case, path = self.prepare(root)
            other = root / "other.json"; other.write_text(json.dumps({"scope": "different", "cases": []}))
            hashes = {p.name: sha256(p.read_bytes()).hexdigest() for p in [case, other]}
            (root / "evidence/foundation/test-results.json").write_text(json.dumps({"success": True, "source_sha256": hashes}))
            first = VerifiedStudy(root, case, path); first.begin(); first.finish({"records": []})
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "case suite changed"):
                VerifiedStudy(root, other, path).begin()
            self.assertEqual(path.read_bytes(), original)

    def test_role_probe_requires_the_authored_role_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); case, path = self.prepare(root)
            role_file = root / "src/kagebunshin/orchestration/roles.json"
            role_file.parent.mkdir(parents=True)
            role_file.write_text(json.dumps([{"id": "R19", "version": 3}]))
            case.write_text(json.dumps({"scope": "role probe", "cases": [{"role_id": "R19", "role_contract_sha256": "old"}]}))
            hashes = {str(p.relative_to(root)): sha256(p.read_bytes()).hexdigest() for p in [case, role_file]}
            (root / "evidence/foundation/test-results.json").write_text(json.dumps({"success": True, "source_sha256": hashes}))
            with self.assertRaisesRegex(ValueError, "role contract differs"):
                VerifiedStudy(root, case, path).begin()
            self.assertFalse(path.exists())

    def test_cli_editions_keep_existing_failed_trial_in_its_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = Path(__file__).resolve().parents[2]
            shutil.copytree(project / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "tools/kagebunshin").mkdir(parents=True)
            shutil.copyfile(project / "tools/kagebunshin/evaluate.py", root / "tools/kagebunshin/evaluate.py")
            old = SQLiteStudyStore(root / "runtime/evaluation/public-pilot-v1/study.sqlite3")
            old.reserve("E01:single", {"state": "failed", "turns": 1})
            def status(study):
                result = subprocess.run([sys.executable, "-I", "tools/kagebunshin/evaluate.py", "--study", study, "status"],
                                        cwd=root, capture_output=True, text=True, timeout=15, check=True)
                return json.loads(result.stdout)
            fresh = status("astra-low-v1")
            self.assertEqual(fresh["trials"], 0)
            self.assertEqual(fresh["study_id"], "astra-low-v1")
            self.assertTrue(fresh["new_edition_is_not_a_new_held_out_dataset"])
            previous = status("public-pilot-v1")
            self.assertEqual(previous["records"], [{"state": "failed", "turns": 1}])
            self.assertEqual(previous["trials"], 1)
            self.assertTrue((root / "runtime/evaluation/astra-low-v1/study.sqlite3").is_file())
            self.assertFalse((root / "evidence").exists())

    def prepare(self, root):
        case = root / "case.json"
        case.write_text(json.dumps({"scope": "receipt preservation fixture", "cases": []}))
        (root / "evidence/foundation").mkdir(parents=True)
        self.verify(root, case)
        return case, root / "evidence/foundation/study.json"

    def verify(self, root, case):
        (root / "evidence/foundation/test-results.json").write_text(json.dumps({
            "success": True, "source_sha256": {case.name: sha256(case.read_bytes()).hexdigest()}}))

    def test_changed_source_stops_batch_before_execution_and_preserves_old_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); case, path = self.prepare(root)
            evidence = VerifiedStudy(root, case, path); evidence.begin()
            evidence.finish({"records": [{"state": "failed", "source_manifest_hash": evidence.manifest_hash}]})
            original = path.read_bytes()
            case.write_text(json.dumps({"scope": "changed definition", "cases": [{"id": "E01"}]}))
            self.verify(root, case)
            def unexpected(*args):
                self.fail("source rejection must precede any job, model preflight, or report publication")
            batch = Batch(unexpected, unexpected, VerifiedStudy(root, case, path), unexpected)
            with self.assertRaisesRegex(ValueError, "implementation changed"):
                batch.run("E01", "single")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob("study*.json")), [path])

    def test_concurrent_progress_snapshots_preserve_first_receipt_and_reuse_identical_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); case, path = self.prepare(root)
            first = VerifiedStudy(root, case, path); first.begin()
            failed = {"state": "failed", "source_manifest_hash": first.manifest_hash}
            first.finish({"records": [failed]}); original = path.read_bytes()
            barrier = Barrier(2)
            report = {"records": [failed, {"state": "finished", "source_manifest_hash": first.manifest_hash}]}
            def publish(_):
                evidence = VerifiedStudy(root, case, path); evidence.begin(); barrier.wait(timeout=5)
                return evidence.finish(report)
            with ThreadPoolExecutor(max_workers=2) as pool:
                written = list(pool.map(publish, range(2)))
            self.assertEqual(written[0], written[1])
            self.assertNotEqual(written[0], path)
            snapshot_bytes = written[0].read_bytes()
            again = VerifiedStudy(root, case, path); again.begin()
            self.assertEqual(again.finish(report), written[0])
            self.assertEqual(written[0].read_bytes(), snapshot_bytes)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(list(path.parent.glob("study*.json"))), 2)
            self.assertEqual(list(path.parent.glob(".study-*")), [])

    def test_mixed_source_report_and_unreadable_receipts_cannot_replace_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); case, path = self.prepare(root)
            evidence = VerifiedStudy(root, case, path); evidence.begin()
            with self.assertRaisesRegex(ValueError, "mixes source"):
                evidence.finish({"records": [{"state": "finished", "source_manifest_hash": "old"}]})
            self.assertFalse(path.exists())
            for payload in (b"not json", b"[]"):
                path.write_bytes(payload)
                with self.assertRaises(ValueError):
                    VerifiedStudy(root, case, path).begin()
                self.assertEqual(path.read_bytes(), payload)
            path.unlink()
            target = root / "external.json"; target.write_text("preserve me")
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                VerifiedStudy(root, case, path).begin()
            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_text(), "preserve me")
