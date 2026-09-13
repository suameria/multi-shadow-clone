from contextlib import closing
import json
from pathlib import Path
import sqlite3

from ..ports import Conflict


class SQLiteDeliveryStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("unsupported delivery schema")
            db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER, snapshot TEXT NOT NULL)")
            initial = {"revision": 0, "epoch": 0, "stopped": False, "operations": {}, "bindings": {}}
            db.execute("INSERT OR IGNORE INTO outbox VALUES(1,0,?)", (json.dumps(initial),))
            if version == 1:
                revision, text = db.execute("SELECT revision,snapshot FROM outbox WHERE id=1").fetchone()
                ledger = json.loads(text)
                ledger["bindings"] = {key: {"binding": op["binding"], "target": key}
                                      for key, op in ledger["operations"].items()}
                ledger["revision"] = revision + 1
                db.execute("UPDATE outbox SET revision=?,snapshot=? WHERE id=1",
                           (revision + 1, json.dumps(ledger, ensure_ascii=False, allow_nan=False)))
            db.execute("PRAGMA user_version=2")
        path.chmod(0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def read(self):
        with closing(self._connect()) as db:
            return json.loads(db.execute("SELECT snapshot FROM outbox WHERE id=1").fetchone()[0])

    def save(self, ledger, expected_revision):
        text = json.dumps({**ledger, "revision": expected_revision + 1}, ensure_ascii=False, allow_nan=False)
        if len(text.encode()) > 20_000_000:
            raise ValueError("delivery ledger bound exceeded")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("UPDATE outbox SET revision=?,snapshot=? WHERE id=1 AND revision=?",
                          (expected_revision + 1, text, expected_revision)).rowcount != 1:
                raise Conflict("delivery revision changed")
