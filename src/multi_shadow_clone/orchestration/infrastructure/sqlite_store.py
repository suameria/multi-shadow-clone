"""SQLite snapshot and append-only event storage, atomically revision guarded."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from ..domain.contracts import canonical
from ..ports import CapacityExhausted, Conflict


class SQLiteRunStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("database symlinks are not supported")
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        with self._connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise ValueError("unsupported database version; do not downgrade")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    row_id INTEGER PRIMARY KEY,
                    public_id TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(public_id),
                    seq INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA secure_delete=ON")
            yield db
        finally:
            db.close()

    def create(self, record: dict) -> None:
        snapshot = canonical(record)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO runs(public_id, revision, state, snapshot) VALUES (?,0,?,?)",
                           (record["id"], record["state"], snapshot))
                self._append_events(db, record, 0)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def read(self, run_id: str) -> dict:
        with self._connection() as db:
            row = db.execute("SELECT snapshot FROM runs WHERE public_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("unknown run")
        return json.loads(row[0])

    def save(self, record: dict, expected_revision: int) -> None:
        updated = {**record, "revision": expected_revision + 1}
        snapshot = canonical(updated)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current = db.execute("SELECT revision,snapshot FROM runs WHERE public_id=?", (record["id"],)).fetchone()
                if current is None or current[0] != expected_revision:
                    raise Conflict("run revision changed")
                old = json.loads(current[1])
                occupied = lambda r: sum(n["active"] is not None for n in r["nodes"].values())
                if occupied(record) > occupied(old):
                    others = db.execute("SELECT snapshot FROM runs WHERE public_id<>?", (record["id"],)).fetchall()
                    if occupied(record) + sum(occupied(json.loads(row[0])) for row in others) > 2:
                        raise CapacityExhausted("at most two active attempts across this store")
                if record["events"][:len(old["events"])] != old["events"]:
                    raise ValueError("event history cannot be rewritten")
                self._append_events(db, record, len(old["events"]))
                db.execute("UPDATE runs SET revision=?,state=?,snapshot=? WHERE public_id=?",
                           (expected_revision + 1, record["state"], snapshot, record["id"]))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _append_events(db, record, after):
        for index, event in enumerate(record["events"][after:], after + 1):
            if event["seq"] != index:
                raise ValueError("non-contiguous event sequence")
            db.execute("INSERT INTO events(run_id,seq,body) VALUES (?,?,?)",
                       (record["id"], index, canonical(event)))

    def list_runs(self) -> list[dict]:
        with self._connection() as db:
            rows = db.execute("SELECT public_id,state,revision FROM runs ORDER BY row_id DESC").fetchall()
        return [{"id": r[0], "state": r[1], "revision": r[2]} for r in rows]

    def erase(self, tombstone: dict, expected_revision: int) -> dict:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute("UPDATE runs SET revision=?,state='erased',snapshot=? WHERE public_id=? AND revision=?",
                                     (expected_revision + 1, canonical({**tombstone, "revision": expected_revision + 1}),
                                      tombstone["id"], expected_revision)).rowcount
                if changed != 1:
                    raise Conflict("run revision changed during erasure")
                db.execute("DELETE FROM events WHERE run_id=?", (tombstone["id"],))
                self._append_events(db, tombstone, 0)
                db.commit()
            except BaseException:
                db.rollback()
                raise
            checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"logical": "erased", "sqlite_pages": "purged" if checkpoint[0] == 0 else "pending_readers",
                "scope": "current SQLite database and WAL; filesystem snapshots and backups excluded"}
