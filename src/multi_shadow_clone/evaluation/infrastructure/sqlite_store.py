from contextlib import closing
import json
from pathlib import Path
import sqlite3


class SQLiteStudyStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink(): raise ValueError("study database symlink rejected")
        path.touch(mode=0o600, exist_ok=True); path.chmod(0o600)
        with closing(self._connect()) as db, db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise ValueError("unsupported study schema")
            db.execute("CREATE TABLE IF NOT EXISTS trials (key TEXT PRIMARY KEY, record TEXT NOT NULL)")
            db.execute("PRAGMA user_version=1")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL"); db.execute("PRAGMA secure_delete=ON")
        return db

    def reserve(self, key, record):
        with closing(self._connect()) as db, db:
            return db.execute("INSERT OR IGNORE INTO trials VALUES (?,?)", (key, json.dumps(record, ensure_ascii=False, allow_nan=False))).rowcount == 1

    def save(self, key, record):
        with closing(self._connect()) as db, db:
            if db.execute("UPDATE trials SET record=? WHERE key=?", (json.dumps(record, ensure_ascii=False, allow_nan=False), key)).rowcount != 1:
                raise ValueError("unknown trial reservation")

    def read(self, key):
        with closing(self._connect()) as db:
            row = db.execute("SELECT record FROM trials WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_records(self):
        with closing(self._connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT record FROM trials ORDER BY key")]
