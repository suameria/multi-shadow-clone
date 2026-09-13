"""Internal registered-check adapter; not assembled into normal BOT dispatch yet."""
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time
from types import MappingProxyType

from ..domain.admission import Rejected, content_hash
from ..domain.checks import PythonCheck
from .supervised_check import run_supervised_check
from .check_workspace import check_workspace
from .macos_sandbox import _canonical, python_check_policy
from .check_journal import CheckJournal
from .check_retirement import retire_check_workspace


_BOOTSTRAP = "import os,runpy,sys; entry=sys.argv[1]; sys.argv=[entry]; sys.path.insert(0,os.getcwd()); runpy.run_path(entry,run_name='__main__')"


class PythonChecks:
    def __init__(self, *, definitions, files, runtime, executable, executable_sha256, snapshot_parent=None, journal_path=None):
        registered = {}
        for definition in definitions:
            if type(definition) is not PythonCheck:
                raise Rejected('invalid check definition')
            definition.validate()
            if definition.check_id in registered:
                raise Rejected('duplicate registered check')
            registered[definition.check_id] = definition
        content_hash(executable_sha256)
        self.definitions = MappingProxyType(registered)
        self.files = files
        self.runtime = _canonical(Path(runtime))
        self.executable = _canonical(Path(executable))
        self.executable_sha256 = executable_sha256
        self.snapshot_parent = snapshot_parent
        self.journal_path = journal_path

    def contract_hash(self, check_id):
        definition = self.definitions.get(check_id)
        if definition is None:
            raise Rejected('unregistered check')
        return definition.fingerprint()

    def run_operation(self, operation_id, payload_hash, check_id, **kwargs):
        if self.journal_path is None:
            raise Rejected('operation checks require a durable journal')
        content_hash(operation_id)
        payload=json.dumps({'operation':'run_check','arguments':{'check_id':check_id}},sort_keys=True,separators=(',',':'),ensure_ascii=False)
        if sha256(payload.encode()).hexdigest()!=payload_hash:
            raise Rejected('check arguments differ from reserved payload')
        return self.run_check(check_id,operation_id=operation_id,payload_hash=payload_hash,**kwargs)

    @staticmethod
    def _receipt(metadata,result):
        return {**metadata,'passed':result['reason']=='completed' and result['returncode']==0,'process':result}

    def completion(self,operation_id,payload_hash,expected_contract):
        if self.journal_path is None:
            raise Rejected('check recovery journal unavailable')
        saved=CheckJournal(self.journal_path).read(operation_id)
        if saved is None:
            return {'state':'not_claimed'}
        context=saved.get('context')
        if (type(context) is not dict or context.get('payload_hash')!=payload_hash
            or type(context.get('receipt')) is not dict
            or context['receipt'].get('definition_hash')!=expected_contract):
            raise Rejected('check recovery context differs from started operation')
        if saved['state']!='completed':
            return {'state':'unknown'}
        return {'state':'completed','receipt':self._receipt(context['receipt'],saved['result'])}

    def retire_operation(self,operation_id,payload_hash,expected_contract):
        if self.completion(operation_id,payload_hash,expected_contract)['state']!='completed':
            raise Rejected('check is not known to have finished')
        journal=CheckJournal(self.journal_path)
        saved=journal.read(operation_id)
        if 'retirement' in saved:
            return saved['retirement']
        ownership=saved['context'].get('workspace')
        if ownership is None:
            raise Rejected('check snapshot ownership unavailable')
        result=retire_check_workspace(ownership)
        journal.record_retirement(operation_id,result)
        return result

    def run_check(self, check_id, *, max_bytes, timeout, stopped, expected_contract=None, operation_id=None, payload_hash=None):
        definition = self.definitions.get(check_id)
        if definition is None:
            raise Rejected('unregistered check')
        if expected_contract is not None and expected_contract != definition.fingerprint():
            raise Rejected('registered check definition changed')
        if (sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file()
            or type(max_bytes) is not int or max_bytes <= 0
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise Rejected('check platform or request bounds unavailable')
        deadline = time.monotonic() + min(timeout, definition.timeout)
        if stopped():
            raise Rejected('check stopped before input read')
        # Check the executable before launch; this does not pin its inode through
        # exec or validate the shared runtime. Both remain dispatch prerequisites.
        with self.executable.open('rb') as executable:
            digest = sha256()
            size = 0
            while chunk := executable.read(65_536):
                size += len(chunk)
                if size > 67_108_864 or time.monotonic() >= deadline or stopped():
                    raise Rejected('check executable verification exceeded bounds or stopped')
                digest.update(chunk)
            if digest.hexdigest() != self.executable_sha256:
                raise Rejected('registered check executable changed')
        input_limit = min(max_bytes, definition.max_input_bytes)
        observations = self.files.read_files(
            [{'path':path,'expected_hash':digest} for path,digest in definition.inputs], input_limit)
        with check_workspace(observations, max_bytes=input_limit, parent=self.snapshot_parent) as snapshot:
            policy = python_check_policy(workspace=snapshot.root, scratch=snapshot.scratch,
                                         runtime=self.runtime, executable=self.executable)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or stopped():
                raise Rejected('check expired or stopped during preparation')
            metadata={'check_id':check_id, 'definition_hash':definition.fingerprint(),
                      'input_hash':snapshot.input_hash,
                      'inputs':[{'path':path,'sha256':digest,'bytes':size} for path,digest,size in snapshot.inputs],
                      'executable_sha256':self.executable_sha256,
                      'policy_sha256':sha256(policy.encode()).hexdigest()}
            result = run_supervised_check(
                argv=['/usr/bin/sandbox-exec','-p',policy,str(self.executable),'-I','-S','-B','-c',_BOOTSTRAP,definition.entrypoint],
                cwd=snapshot.root,
                env={'PATH':'/usr/bin:/bin','HOME':str(snapshot.scratch),'TMPDIR':str(snapshot.scratch)},
                timeout=remaining, max_output_bytes=min(max_bytes,definition.max_output_bytes), stopped=stopped,
                journal_path=self.journal_path if operation_id is not None else None,operation_id=operation_id,
                journal_context={'payload_hash':payload_hash,'receipt':metadata,'workspace':snapshot.ownership} if operation_id is not None else None)
            return self._receipt(metadata,result)
