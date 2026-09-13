"""Actual knowledge and job SQLite stores across the public application boundary."""
from pathlib import Path
import tempfile
import unittest

from kagebunshin.knowledge.application.library import Library
from kagebunshin.knowledge.application.workflow import KnowledgeWorkflow
from kagebunshin.knowledge.infrastructure.job_bridge import EvidenceJobs
from kagebunshin.knowledge.infrastructure.sqlite_store import SQLiteKnowledgeStore
from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.infrastructure.sqlite_store import SQLiteRunStore
from tests.unit.fakes import ROLES, ScriptedProvider


class EvidenceSQLiteTest(unittest.TestCase):
    def test_crash_gap_recovery_erases_both_databases_and_wal(self):
        marker = "UNIQUE_OWNED_ERASURE_MARKER_83f476"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            library = Library(SQLiteKnowledgeStore(root / "knowledge.sqlite3"))
            sid = library.register(title="fixture", url="local:owned", content=marker, coverage="all", rights="owned", accessed_at="2026-09-13")
            cid = library.claim(marker, [sid])
            provider = ScriptedProvider()
            engine = Engine(SQLiteRunStore(root / "jobs.sqlite3"), provider, ROLES, lambda: 1000, library)
            workflow = KnowledgeWorkflow(library, EvidenceJobs(engine))
            job = workflow.handoff(marker, [cid])["run_id"]
            self.assertEqual(provider.requests, [])
            self.assertIn(marker, str(engine.status(job)))
            # Simulated process gap: local erasure persists before consumer erasure.
            library.erase(sid)
            restarted = Engine(SQLiteRunStore(root / "jobs.sqlite3"), provider, ROLES, lambda: 1000, library)
            receipt = KnowledgeWorkflow(library, EvidenceJobs(restarted)).erase(sid)
            self.assertFalse(receipt["all_copies_erased"])
            self.assertEqual(receipt["consumers"]["jobs"][0]["local"]["sqlite_pages"], "purged")
            self.assertEqual(restarted.status(job)["state"], "erased")
            for file in root.iterdir():
                self.assertNotIn(marker.encode(), file.read_bytes(), file.name)
