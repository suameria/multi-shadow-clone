"""Atomic container reservations; persisted intent is not proof of an effect."""
from contextlib import closing
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3

from ..domain.admission import Rejected, content_hash
from ..domain.container_lifecycle import transition


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


class ContainerJournal:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink():
            raise Rejected('container journal cannot be a symlink')
        self.path.touch(mode=0o600,exist_ok=True)
        self.path.chmod(0o600)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS containers (id TEXT PRIMARY KEY, body TEXT NOT NULL, hash TEXT NOT NULL)')

    @staticmethod
    def _read(db,operation_id):
        row=db.execute('SELECT body,hash FROM containers WHERE id=?',(operation_id,)).fetchone()
        if row is None:
            return None
        if sha256(row[0].encode()).hexdigest()!=row[1]:
            raise Rejected('container journal checksum differs')
        return json.loads(row[0])

    def read(self,operation_id):
        content_hash(operation_id)
        with closing(sqlite3.connect(self.path)) as db:
            return self._read(db,operation_id)

    def _change(self,operation_id,change):
        content_hash(operation_id)
        with closing(sqlite3.connect(self.path,timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            state=change(self._read(db,operation_id))
            body=encode(state)
            db.execute('INSERT INTO containers VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,hash=excluded.hash',
                       (operation_id,body,sha256(body.encode()).hexdigest()))
        return json.loads(body)

    def claim(self,operation_id,config,*,context=None):
        frozen=json.loads(encode(config))
        frozen_context=json.loads(encode(context))
        def claim(current):
            if current is not None:
                raise Rejected('container claim exists; reconcile instead')
            return {'phase':'claimed','config':frozen,'config_hash':sha256(encode(frozen).encode()).hexdigest(),'context':frozen_context}
        return self._change(operation_id,claim)

    def advance(self,operation_id,config,event,**observation):
        expected=sha256(encode(config).encode()).hexdigest()
        def advance(current):
            if current is None or current['config_hash']!=expected:
                raise Rejected('container configuration differs from claim')
            return transition(current,event,**observation)
        return self._change(operation_id,advance)

    def reserve_attached(self,operation_id,config,*,timeout,max_output_bytes):
        if (type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=300
            or type(max_output_bytes) is not int or not 0<max_output_bytes<=1048576):
            raise Rejected('invalid container execution bounds')
        expected=sha256(encode(config).encode()).hexdigest()
        def reserve(current):
            if current is None or current['config_hash']!=expected:
                raise Rejected('container execution differs from claim')
            state=transition(current,'reserve_start')
            state['limits']={'timeout':timeout,'max_output_bytes':max_output_bytes}
            return state
        return self._change(operation_id,reserve)

    def record_output(self,operation_id,config,result):
        expected=sha256(encode(config).encode()).hexdigest()
        frozen=json.loads(encode(result))
        def record(current):
            if (current is None or current['config_hash']!=expected or 'limits' not in current
                or current['phase'] not in ('starting','running','stopping','exited')):
                raise Rejected('container output has no execution reservation')
            maximum=current['limits']['max_output_bytes']
            if (type(frozen.get('output_bytes')) is not int or not 0<=frozen['output_bytes']<=maximum
                or len(encode(frozen).encode())>maximum*8+8192):
                raise Rejected('container output exceeds reservation')
            if 'output' in current and current['output']!=frozen:
                raise Rejected('container output is immutable')
            current['output']=frozen
            return current
        return self._change(operation_id,record)

    def claim_supervisor(self,operation_id,config,pid):
        expected=sha256(encode(config).encode()).hexdigest()
        if type(pid) is not int or pid<=0:
            raise Rejected('invalid container supervisor identity')
        def claim(current):
            if (current is None or current['config_hash']!=expected or current['phase']!='created'
                or 'supervisor' in current):
                raise Rejected('container supervision must not be repeated')
            current['supervisor']={'pid':pid}
            return current
        return self._change(operation_id,claim)

    def complete_supervisor(self,operation_id,config,pid,reason):
        expected=sha256(encode(config).encode()).hexdigest()
        if reason not in ('completed','stopped','owner_lost','deadline'):
            raise Rejected('invalid container supervision result')
        def complete(current):
            if (current is None or current['config_hash']!=expected or current['phase']!='exited'
                or current.get('supervisor',{}).get('pid')!=pid):
                raise Rejected('container supervision outcome is unknown')
            previous=current['supervisor'].get('reason')
            if previous is not None and previous!=reason:
                raise Rejected('container supervision result is immutable')
            current['supervisor']['reason']=reason
            return current
        return self._change(operation_id,complete)

    def record_snapshot_retirement(self,operation_id,result):
        frozen=json.loads(encode(result))
        def retire(current):
            if current is None or current['phase']!='retired':
                raise Rejected('container must be retired before its snapshot')
            if 'snapshot_retirement' in current and current['snapshot_retirement']!=frozen:
                raise Rejected('snapshot retirement is immutable')
            current['snapshot_retirement']=frozen
            return current
        return self._change(operation_id,retire)
