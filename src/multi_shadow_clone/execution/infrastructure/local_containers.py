"""Explicit local Docker CLI adapter, with no inherited context or credentials."""
from dataclasses import asdict
from contextlib import contextmanager
from hashlib import sha256
import json
import math
from pathlib import Path
import stat
import tempfile
import time

from ..domain.admission import Rejected, content_hash
from .check_process import run_check_process


class LocalContainers:
    def __init__(self,*,executable,executable_hash,socket_path,server_id,server_version,spec):
        self.executable=Path(executable).resolve(strict=True)
        self.socket=Path(socket_path).resolve(strict=True)
        content_hash(executable_hash)
        if not stat.S_ISSOCK(self.socket.stat().st_mode):
            raise Rejected('Docker endpoint is not a local socket')
        if not server_id or not server_version:
            raise Rejected('Docker identity and version must be explicit')
        spec.validate()
        self.executable_hash=executable_hash
        self.server_id=server_id
        self.server_version=server_version
        self.spec=spec
        self._budget=None

    @contextmanager
    def execution_scope(self,timeout,stopped):
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=300:
            raise Rejected('invalid container execution deadline')
        if self._budget is not None:
            raise Rejected('container runtime is already executing')
        self._budget=(time.monotonic()+timeout,stopped)
        try:
            yield
        finally:
            self._budget=None

    def contract(self):
        # Return JSON-stable values for restart comparisons.
        return json.loads(json.dumps({'kind':'local-container-v1','executable':str(self.executable),
            'executable_hash':self.executable_hash,'socket':str(self.socket),'server_id':self.server_id,
            'server_version':self.server_version,'spec':asdict(self.spec)}))

    def _command(self,arguments):
        result=self._bounded(arguments,timeout=10,max_output_bytes=1048576,stopped=lambda:False)
        if result['reason']!='completed' or result['returncode']!=0:
            raise Rejected('Docker command outcome requires reconciliation')
        return result['stdout']

    def _bounded(self,arguments,*,timeout,max_output_bytes,stopped):
        if self._budget is not None:
            deadline,budget_stopped=self._budget
            if budget_stopped() or time.monotonic()>=deadline:
                raise Rejected('container execution budget ended')
        if self.executable.stat().st_size>128*1024*1024:
            raise Rejected('Docker executable exceeds verification bound')
        if sha256(self.executable.read_bytes()).hexdigest()!=self.executable_hash:
            raise Rejected('Docker executable changed')
        if not stat.S_ISSOCK(self.socket.stat().st_mode):
            raise Rejected('Docker socket changed')
        with tempfile.TemporaryDirectory(prefix='multi-shadow-clone-docker-config-') as directory:
            if self._budget is not None:
                timeout=min(timeout,deadline-time.monotonic())
                original_stopped=stopped
                stopped=lambda:budget_stopped() or original_stopped()
            result=run_check_process(argv=[str(self.executable),'--host','unix://'+str(self.socket),
                '--config',directory,*arguments],cwd=directory,
                env={'PATH':'/usr/bin:/bin','HOME':directory},timeout=timeout,max_output_bytes=max_output_bytes,
                stopped=stopped)
        return result

    def _verify_daemon(self):
        info=json.loads(self._command(['info','--format','{{json .}}']))
        if (info.get('ID')!=self.server_id or info.get('ServerVersion')!=self.server_version
            or info.get('OSType')!='linux' or info.get('CgroupVersion')!='2'):
            raise Rejected('Docker daemon identity or capability changed')

    def create(self):
        self._verify_daemon()
        container_id=self._command(self.spec.create_arguments()).strip()
        content_hash(container_id)
        return container_id

    def inspect(self):
        self._verify_daemon()
        lines=self._command(['container','ls','--all','--no-trunc','--filter',
            'name=^/multi-shadow-clone-'+self.spec.owner+'$','--format','{{.ID}}']).splitlines()
        if not lines:
            return None
        if len(lines)!=1:
            raise Rejected('container ownership is ambiguous')
        content_hash(lines[0])
        items=json.loads(self._command(['container','inspect',lines[0]]))
        if type(items) is not list or len(items)!=1 or items[0].get('Id')!=lines[0]:
            raise Rejected('Docker inspection differs from selected identity')
        return items[0]

    def verify_created(self,observed):
        return self.spec.verify_created(observed['Id'],observed)

    def verify_owned(self,observed):
        return self.spec.verify_owned(observed['Id'],observed)

    def remove(self,container_id):
        content_hash(container_id)
        observed=self.inspect()
        if observed is None or observed['Id']!=container_id:
            raise Rejected('container removal identity differs')
        self.verify_owned(observed)
        # No --force: the daemon must reject a concurrently started container.
        return self._command(['container','rm',container_id]).strip()

    def start(self,container_id):
        content_hash(container_id)
        observed=self.inspect()
        if observed is None or observed['Id']!=container_id:
            raise Rejected('container start identity differs')
        self.verify_created(observed)
        return self._command(['container','start',container_id]).strip()

    def kill(self,container_id):
        content_hash(container_id)
        observed=self.inspect()
        if observed is None or observed['Id']!=container_id:
            raise Rejected('container stop identity differs')
        self.verify_owned(observed)
        if observed['State'].get('Running') is not True:
            return None
        return self._command(['container','kill','--signal','KILL',container_id]).strip()

    def start_attached(self,container_id,*,timeout,max_output_bytes,stopped):
        content_hash(container_id)
        observed=self.inspect()
        if observed is None or observed['Id']!=container_id:
            raise Rejected('attached start identity differs')
        self.verify_created(observed)
        return self._bounded(['container','start','--attach',container_id],timeout=timeout,
                             max_output_bytes=max_output_bytes,stopped=stopped)
