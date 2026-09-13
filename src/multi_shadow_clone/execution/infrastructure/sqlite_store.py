"""Serialize grant admission and STOP under a single SQLite write transaction."""
from hashlib import sha256
import json
import sqlite3


class SQLiteOperationStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError('operation database cannot be a symlink')
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS operation_grants (id TEXT PRIMARY KEY, body TEXT NOT NULL, hash TEXT NOT NULL)')

    @staticmethod
    def _read(db, grant_id):
        row = db.execute('SELECT body,hash FROM operation_grants WHERE id=?', (grant_id,)).fetchone()
        if row is None:
            return None
        if sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError('operation state hash mismatch')
        return json.loads(row[0])

    def read(self, grant_id):
        with sqlite3.connect(self.path) as db:
            result = self._read(db, grant_id)
        if result is None:
            raise KeyError(grant_id)
        return result

    def transact(self, grant_id, change):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            state = change(self._read(db, grant_id))
            if state['grant']['binding']['grant_id'] != grant_id:
                raise ValueError('grant identity changed')
            body = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            db.execute('INSERT INTO operation_grants VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,hash=excluded.hash',
                       (grant_id, body, sha256(body.encode()).hexdigest()))
        return json.loads(body)
