"""One authoritative local graph snapshot; no stale secondary text index."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from ..ports import Conflict


def empty_book() -> dict:
    return {"revision": 0, "sources": {}, "claims": {}, "relations": {}, "consumers": {}, "erasures": {}}


class SQLiteKnowledgeStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("database symlinks are not supported")
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        with closing(self._connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported knowledge schema")
            db.execute("CREATE TABLE IF NOT EXISTS library (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER, snapshot TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO library VALUES (1,0,?)", (json.dumps(empty_book()),))
            db.execute("PRAGMA user_version=1")
        path.chmod(0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def read(self) -> dict:
        with closing(self._connect()) as db:
            return json.loads(db.execute("SELECT snapshot FROM library WHERE id=1").fetchone()[0])

    def save(self, book: dict, expected_revision: int) -> None:
        text = json.dumps({**book, "revision": expected_revision + 1}, ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE library SET revision=?,snapshot=? WHERE id=1 AND revision=?",
                                 (expected_revision + 1, text, expected_revision)).rowcount
            if changed != 1:
                raise Conflict("knowledge revision changed")
