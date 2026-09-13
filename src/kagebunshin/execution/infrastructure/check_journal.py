"""Supervisor-owned durable lifecycle for one exact host check configuration."""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from ..domain.admission import Rejected, content_hash


def _encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


class CheckJournal:
    def __init__(self,path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink():
            raise Rejected('check journal cannot be a symlink')
        self.path.touch(mode=0o600,exist_ok=True)
        self.path.chmod(0o600)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS check_runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, hash TEXT NOT NULL)')

    @staticmethod
    def _read(db,operation_id):
        row=db.execute('SELECT body,hash FROM check_runs WHERE id=?',(operation_id,)).fetchone()
        if row is None:
            return None
        if sha256(row[0].encode()).hexdigest()!=row[1]:
            raise Rejected('check journal changed')
        return json.loads(row[0])

    def _change(self,operation_id,change):
        content_hash(operation_id)
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            state=change(self._read(db,operation_id))
            body=_encoded(state)
            db.execute('INSERT INTO check_runs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,hash=excluded.hash',
                       (operation_id,body,sha256(body.encode()).hexdigest()))
        return json.loads(body)

    def claim(self,operation_id,config,*,context=None):
        encoded=_encoded(config)
        frozen=json.loads(encoded)
        frozen_context=json.loads(_encoded(context))
        def claim(current):
            if current is not None:
                raise Rejected('check already claimed; inspect instead of rerunning')
            return {'state':'claimed','config':frozen,'config_hash':sha256(encoded.encode()).hexdigest(),'context':frozen_context}
        return self._change(operation_id,claim)

    def start(self,operation_id,config,supervisor_pid):
        expected=sha256(_encoded(config).encode()).hexdigest()
        def start(current):
            if current is None or current['state']!='claimed' or current['config_hash']!=expected:
                raise Rejected('check start differs from durable claim')
            current.update(state='running',supervisor_pid=supervisor_pid)
            return current
        return self._change(operation_id,start)

    def complete(self,operation_id,config,result):
        expected=sha256(_encoded(config).encode()).hexdigest()
        result=json.loads(_encoded(result))
        def complete(current):
            if current is None or current['config_hash']!=expected:
                raise Rejected('check completion differs from claim')
            if current['state']=='completed' and current['result']==result:
                return current
            if current['state']!='running':
                raise Rejected('check completion is not awaiting a result')
            current.update(state='completed',result=result)
            return current
        return self._change(operation_id,complete)

    def read(self,operation_id):
        content_hash(operation_id)
        with sqlite3.connect(self.path) as db:
            return self._read(db,operation_id)

    def record_retirement(self,operation_id,result):
        frozen=json.loads(_encoded(result))
        def retire(current):
            if current is None or current['state']!='completed':
                raise Rejected('check completion must be known before retirement')
            if 'retirement' in current and current['retirement']!=frozen:
                raise Rejected('check retirement receipt is immutable')
            current['retirement']=frozen
            return current
        return self._change(operation_id,retire)
