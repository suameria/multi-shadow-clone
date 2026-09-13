"""Durable completion receipts, separate from orchestration's result save."""
from hashlib import sha256
import json
import sqlite3
from ..domain.admission import Rejected, content_hash


class WriteJournal:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if path.is_symlink(): raise Rejected('journal symlink is unsupported')
        path.touch(mode=0o600,exist_ok=True)
        path.chmod(0o600)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS writes (id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, receipt TEXT, receipt_hash TEXT)')

    def claim(self, operation_id, payload_hash):
        content_hash(operation_id)
        content_hash(payload_hash)
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT payload_hash FROM writes WHERE id=?',(operation_id,)).fetchone()
            if existing is not None:
                raise Rejected('journaled write must be inspected, never executed again')
            db.execute('INSERT INTO writes(id,payload_hash) VALUES (?,?)',(operation_id,payload_hash))

    def complete(self, operation_id, payload_hash, receipt):
        body = json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload_hash,receipt FROM writes WHERE id=?',(operation_id,)).fetchone()
            if row is None or row[0] != payload_hash or (row[1] is not None and row[1] != body):
                raise Rejected('completion does not match journal claim')
            db.execute('UPDATE writes SET receipt=?,receipt_hash=? WHERE id=?',(body,sha256(body.encode()).hexdigest(),operation_id))

    def read(self, operation_id, payload_hash):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT payload_hash,receipt,receipt_hash FROM writes WHERE id=?',(operation_id,)).fetchone()
        if row is None: return {'state':'not_claimed'}
        if row[0] != payload_hash: raise Rejected('journal payload mismatch')
        if row[1] is None: return {'state':'unknown'}
        if sha256(row[1].encode()).hexdigest() != row[2]: raise Rejected('journal receipt changed')
        return {'state':'completed','receipt':json.loads(row[1])}


class JournaledWriter:
    def __init__(self, files, journal):
        self.files,self.journal = files,journal

    def apply_operation(self, operation_id, payload_hash, arguments, max_bytes):
        payload = json.dumps({"operation":"apply_changes","arguments":arguments},sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)
        if sha256(payload.encode()).hexdigest() != payload_hash:
            raise Rejected("write arguments differ from journal payload")
        self.journal.claim(operation_id,payload_hash)
        receipt = self.files.apply_change(**arguments,max_bytes=max_bytes)
        self.journal.complete(operation_id,payload_hash,receipt)
        return receipt

    def inspect_file(self, path, max_bytes):
        return self.files.inspect_file(path,max_bytes)

    def completion(self, operation_id, payload_hash):
        return self.journal.read(operation_id,payload_hash)
