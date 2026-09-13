"""Append-only model settings versions and a CAS-controlled current pointer."""
import json
from pathlib import Path
import sqlite3

from ..domain.contracts import canonical, digest
from ..ports import Conflict


class SQLiteSettingsStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("settings database symlink is unsupported")
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings_versions (revision INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL)")

    def read(self, revision=None):
        with sqlite3.connect(self.path) as db:
            row = (db.execute("SELECT revision,fingerprint,body FROM settings_versions ORDER BY revision DESC LIMIT 1").fetchone()
                   if revision is None else db.execute("SELECT revision,fingerprint,body FROM settings_versions WHERE revision=?", (revision,)).fetchone())
        if row is None:
            if revision is not None:
                raise KeyError("settings revision not found")
            return {"revision": 0, "value": None}
        value = json.loads(row[2])
        if digest(value) != row[1]:
            raise ValueError("settings fingerprint mismatch")
        return {"revision": row[0], "fingerprint": row[1], "value": value}

    def save(self, value, expected_revision):
        body = canonical(value)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM settings_versions").fetchone()[0]
            if current != expected_revision:
                raise Conflict("settings changed; refresh before saving")
            revision = current + 1
            db.execute("INSERT INTO settings_versions VALUES (?,?,?)", (revision, digest(value), body))
        return self.read(revision)
