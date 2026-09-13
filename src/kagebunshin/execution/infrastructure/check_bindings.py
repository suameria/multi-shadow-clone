"""Immutable attempt input bindings, separate from check process outcomes."""
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from ..domain.admission import Rejected, content_hash
from ..domain.checks import NodeCheck


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


class CheckBindings:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink():raise Rejected('binding database cannot be a symlink')
        self.path.touch(mode=0o600,exist_ok=True);self.path.chmod(0o600)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS check_bindings (attempt TEXT PRIMARY KEY, body TEXT NOT NULL, hash TEXT NOT NULL)')

    @staticmethod
    def identity(attempt,contract_hash):
        if type(attempt) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',attempt):
            raise Rejected('invalid attempt identity')
        content_hash(contract_hash)

    @staticmethod
    def _read(db,attempt):
        row=db.execute('SELECT body,hash FROM check_bindings WHERE attempt=?',(attempt,)).fetchone()
        if row is None:return None
        if sha256(row[0].encode()).hexdigest()!=row[1]:raise Rejected('check binding record changed')
        return json.loads(row[0])

    def save(self,attempt,contract_hash,definitions):
        self.identity(attempt,contract_hash)
        definitions=tuple(definitions)
        if not 1<=len(definitions)<=100 or any(type(d) is not NodeCheck for d in definitions):
            raise Rejected('invalid attempt check definitions')
        for definition in definitions:definition.validate()
        if len({d.check_id for d in definitions})!=len(definitions):raise Rejected('duplicate bound check')
        record={'contract_hash':contract_hash,'definitions':[asdict(d) for d in sorted(definitions,key=lambda d:d.check_id)]}
        body=encode(record)
        with closing(sqlite3.connect(self.path,timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous=self._read(db,attempt)
            if previous is not None and encode(previous)!=body:raise Rejected('attempt cannot bind different check inputs')
            if previous is None:
                db.execute('INSERT INTO check_bindings VALUES (?,?,?)',(attempt,body,sha256(body.encode()).hexdigest()))
        return self.read(attempt,contract_hash)

    def read(self,attempt,contract_hash):
        self.identity(attempt,contract_hash)
        with closing(sqlite3.connect(self.path)) as db:
            record=self._read(db,attempt)
        if record is None:return None
        if record['contract_hash']!=contract_hash:raise Rejected('attempt contract changed')
        definitions=[]
        for value in record['definitions']:
            value={**value,'inputs':tuple(tuple(item) for item in value['inputs'])}
            definition=NodeCheck(**value);definition.validate();definitions.append(definition)
        return tuple(definitions)
