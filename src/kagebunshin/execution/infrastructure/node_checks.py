"""Registered Node checks backed by an owned container and durable receipts."""
from hashlib import sha256
import json
import math
import secrets
import time
from types import MappingProxyType

from ..domain.admission import Rejected, content_hash
from ..domain.checks import NodeCheck, CheckOutcomeUnknown
from .check_workspace import check_workspace
from .check_retirement import retire_check_workspace
from .docker_sandbox import NodeContainer, SnapshotMount
from .node_inputs import node_arguments
from .supervised_container import run_supervised_container


def fingerprint(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class NodeChecks:
    def __init__(self,*,definitions,files,profile,service_factory,journal,snapshot_parent=None):
        registered={}
        for definition in definitions:
            if type(definition) is not NodeCheck or definition.check_id in registered:
                raise Rejected('invalid or duplicate Node check')
            definition.validate()
            registered[definition.check_id]=definition
        self.definitions=MappingProxyType(registered)
        self.files=files
        self.profile=json.loads(json.dumps(profile,allow_nan=False))
        self.service_factory=service_factory
        self.journal=journal
        self.snapshot_parent=snapshot_parent

    def contract_hash(self,check_id):
        if check_id not in self.definitions:
            raise Rejected('unregistered Node check')
        return fingerprint({'definition':self.definitions[check_id].fingerprint(),'profile':self.profile})

    def run_operation(self,operation_id,payload_hash,check_id,*,expected_contract,max_bytes,timeout,stopped):
        content_hash(operation_id)
        payload={'operation':'run_check','arguments':{'check_id':check_id}}
        if (fingerprint(payload)!=payload_hash or self.contract_hash(check_id)!=expected_contract
            or self.journal.read(operation_id) is not None):
            raise Rejected('Node operation differs or already exists; recover instead')
        if (type(max_bytes) is not int or max_bytes<=0 or type(timeout) not in (int,float)
            or not math.isfinite(timeout) or timeout<=0 or stopped()):
            raise Rejected('Node operation bounds unavailable or stopped')
        definition=self.definitions[check_id]
        deadline=time.monotonic()+min(timeout,definition.timeout)
        limit=min(max_bytes,definition.max_input_bytes)
        observations=self.files.read_files([{'path':path,'expected_hash':digest} for path,digest in definition.inputs],limit)
        with check_workspace(observations,max_bytes=limit,parent=self.snapshot_parent) as snapshot:
            info=snapshot.root.stat()
            mount=SnapshotMount(str(snapshot.root),info.st_dev,info.st_ino,snapshot.ownership['marker_hash'],info.st_uid,info.st_gid)
            spec=NodeContainer(self.profile['image_id'],secrets.token_hex(16),node_arguments(snapshot.inputs,definition.entrypoint),mount)
            service=self.service_factory(spec)
            contract=service.runtime.contract()
            actual={key:value for key,value in contract.items() if key!='spec'}
            actual['image_id']=contract['spec']['image_id']
            if actual!=self.profile or service.journal.path!=self.journal.path:
                raise Rejected('Node runtime differs from registered profile')
            metadata={'check_id':check_id,'definition_hash':expected_contract,'input_hash':snapshot.input_hash,
                'inputs':[{'path':path,'sha256':digest,'bytes':size} for path,digest,size in snapshot.inputs],
                'runtime_hash':fingerprint(contract)}
            context={'payload_hash':payload_hash,'receipt':metadata,'workspace':snapshot.ownership}
            try:
                if stopped() or time.monotonic()>=deadline:
                    raise Rejected('Node preparation expired or stopped')
                service.prepare(operation_id,context=context)
                run_supervised_container(runtime=contract,journal_path=self.journal.path,operation_id=operation_id,
                    timeout=deadline-time.monotonic(),max_output_bytes=min(max_bytes,definition.max_output_bytes),stopped=stopped)
                service.retire(operation_id)
                receipt=self.completion(operation_id,payload_hash,expected_contract)['receipt']
            except BaseException as exc:
                saved=self.journal.read(operation_id)
                if saved is not None and saved.get('context')==context:
                    if isinstance(exc,CheckOutcomeUnknown):
                        raise
                    raise CheckOutcomeUnknown('Node operation requires reconciliation') from exc
                raise
        self.retire_operation(operation_id,payload_hash,expected_contract)
        return receipt

    def completion(self,operation_id,payload_hash,expected_contract):
        saved=self.journal.read(operation_id)
        if saved is None:
            return {'state':'not_claimed'}
        context=saved.get('context')
        if (type(context) is not dict or context.get('payload_hash')!=payload_hash
            or context.get('receipt',{}).get('definition_hash')!=expected_contract
            or context['receipt'].get('runtime_hash')!=fingerprint(saved['config'])):
            raise Rejected('Node receipt binding differs')
        if saved['phase'] not in ('exited','retiring','retired') or 'output' not in saved or 'result' not in saved:
            return {'state':'unknown'}
        if 'reason' not in saved.get('supervisor',{}):
            return {'state':'unknown'}
        passed=(saved['supervisor']['reason']=='completed' and saved['output']['reason']=='completed'
                and saved['output']['returncode']==0 and saved['result']['exit_code']==0
                and saved['result']['oom_killed'] is False)
        return {'state':'completed','receipt':{**context['receipt'],'passed':passed,
            'process':saved['output'],'container_result':saved['result'],'supervision':saved['supervisor']['reason']}}

    def retire_operation(self,operation_id,payload_hash,expected_contract):
        if self.completion(operation_id,payload_hash,expected_contract)['state']!='completed':
            raise Rejected('Node outcome remains unknown')
        saved=self.journal.read(operation_id)
        if 'snapshot_retirement' in saved:
            return saved['snapshot_retirement']
        if saved['phase']!='retired':
            specification=dict(saved['config']['spec'])
            specification['arguments']=tuple(specification['arguments'])
            specification['snapshot']=SnapshotMount(**specification['snapshot'])
            service=self.service_factory(NodeContainer(**specification))
            service.retire(operation_id)
        result=retire_check_workspace(saved['context']['workspace'])
        self.journal.record_snapshot_retirement(operation_id,result)
        return result
