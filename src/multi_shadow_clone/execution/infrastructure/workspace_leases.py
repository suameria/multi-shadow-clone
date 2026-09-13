"""Durable workspace ownership; process exit alone never releases a lease."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from ..domain.admission import Rejected, content_hash


class WorkspaceLeases:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink():raise Rejected('lease database cannot be a symlink')
        self.path.touch(mode=0o600,exist_ok=True);self.path.chmod(0o600)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS workspace_leases (workspace TEXT PRIMARY KEY, body TEXT NOT NULL, hash TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_releases (workspace TEXT, run TEXT, body TEXT NOT NULL, hash TEXT NOT NULL, PRIMARY KEY(workspace,run))')

    def read(self,identity,run_id=None):
        key=self.key(identity)
        with closing(sqlite3.connect(self.path)) as db:
            if run_id is not None:
                row=db.execute('SELECT body,hash FROM workspace_releases WHERE workspace=? AND run=?',(key,run_id)).fetchone()
                if row:
                    if sha256(row[0].encode()).hexdigest()!=row[1]:raise Rejected('release history changed')
                    return json.loads(row[0])
            return self._read(db,key)

    @staticmethod
    def key(identity):
        if (type(identity) is not dict or set(identity)!={'device','inode'}
            or any(type(v) is not int or v<0 for v in identity.values())):
            raise Rejected('invalid workspace identity')
        return json.dumps(identity,sort_keys=True,separators=(',',':'))

    @staticmethod
    def _read(db,key):
        row=db.execute('SELECT body,hash FROM workspace_leases WHERE workspace=?',(key,)).fetchone()
        if row is None:return None
        if sha256(row[0].encode()).hexdigest()!=row[1]:raise Rejected('workspace lease changed')
        return json.loads(row[0])

    @staticmethod
    def _write(db,key,value):
        body=json.dumps(value,sort_keys=True,separators=(',',':'))
        db.execute('INSERT INTO workspace_leases VALUES (?,?,?) ON CONFLICT(workspace) DO UPDATE SET body=excluded.body,hash=excluded.hash',(key,body,sha256(body.encode()).hexdigest()))

    def claim(self,identity,run_id,contract_hash):
        key=self.key(identity);content_hash(contract_hash)
        if type(run_id) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',run_id):raise Rejected('invalid lease owner')
        with closing(sqlite3.connect(self.path,timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE');previous=self._read(db,key)
            if db.execute('SELECT 1 FROM workspace_releases WHERE workspace=? AND run=?',(key,run_id)).fetchone():
                raise Rejected('retired job cannot reacquire workspace')
            if previous and previous['state']=='held':
                if (previous['run_id'],previous['contract_hash'])!=(run_id,contract_hash):raise Rejected('workspace belongs to another job or contract')
                return previous
            value={'run_id':run_id,'contract_hash':contract_hash,'generation':1 if previous is None else previous['generation']+1,'state':'held'}
            self._write(db,key,value);return value

    def owns(self,identity,lease):
        key=self.key(identity)
        with closing(sqlite3.connect(self.path)) as db:
            current=self._read(db,key)
        return current==lease and current is not None and current['state']=='held'

    def release(self,identity,lease,retirement_hash):
        """Host supplies the verified retirement receipt hash; this store does not infer completion."""
        key=self.key(identity);content_hash(retirement_hash)
        with closing(sqlite3.connect(self.path,timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE');current=self._read(db,key)
            if current is None:raise Rejected('unknown workspace lease')
            if current['state']=='released':
                if any(current.get(k)!=lease.get(k) for k in ('run_id','contract_hash','generation')) or current.get('retirement_hash')!=retirement_hash:
                    raise Rejected('release receipt or owner differs')
                return current
            if current!=lease:raise Rejected('lease no longer owned')
            value={**current,'state':'released','retirement_hash':retirement_hash}
            body=json.dumps(value,sort_keys=True,separators=(',',':'))
            db.execute('INSERT INTO workspace_releases VALUES (?,?,?,?)',(key,current['run_id'],body,sha256(body.encode()).hexdigest()))
            self._write(db,key,value);return value
