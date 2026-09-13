from pathlib import Path
import tempfile
import unittest

from multi_shadow_clone.knowledge.application.library import Library
from multi_shadow_clone.knowledge.infrastructure.sqlite_store import SQLiteKnowledgeStore
from multi_shadow_clone.knowledge.ports import Conflict


class KnowledgeSQLiteTest(unittest.TestCase):
    def test_restart_cas_and_erasure_use_real_product_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "knowledge.sqlite3"
            store = SQLiteKnowledgeStore(path)
            library = Library(store)
            sid = library.register(title="owned fixture", url="https://example.test", content="sensitive-synthetic-marker",
                                   coverage="fixture", rights="test-owned", accessed_at="2026-09-13")
            cid = library.claim("sensitive-synthetic-marker claim", [sid])
            stale = store.read()
            restarted = Library(SQLiteKnowledgeStore(path))
            self.assertEqual(restarted.bundle([cid])["sources"][sid]["content"], "sensitive-synthetic-marker")
            restarted.erase(sid)
            with self.assertRaises(Conflict):
                store.save(stale, stale["revision"])
            self.assertNotIn(b"sensitive-synthetic-marker", path.read_bytes())
            self.assertFalse(path.with_name(path.name + "-wal").exists())
