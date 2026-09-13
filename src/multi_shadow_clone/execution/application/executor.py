"""Execute saved operations through host-bound workspace capabilities."""
from hashlib import sha256
from dataclasses import asdict
import json
from types import MappingProxyType
from ..domain.admission import Rejected
from ..domain.checks import CheckOutcomeUnknown
from .reservations import Reservations


class Executor:
    def __init__(self, reservations: Reservations, readers, *, writers=None, checkers=None):
        self.reservations = reservations
        self.readers = MappingProxyType(dict(readers))
        self.writers = MappingProxyType(dict(writers or {}))
        self.checkers = MappingProxyType(dict(checkers or {}))

    def _stopped(self, binding, may_continue):
        if may_continue is not None:
            try:
                allowed = may_continue() is True
            except Exception:
                allowed = False
            if not allowed:
                self.reservations.stop(binding.grant_id)
        return self.reservations.execution_stopped(binding)

    def execute(self, binding, call_id, *, may_continue=None):
        current = self.reservations.store.read(binding.grant_id)
        call = current['calls'].get(call_id)
        if call is None:
            raise Rejected('operation is not reserved')
        operation = call['operation']
        adapters = {'read_files':self.readers,'apply_changes':self.writers,'run_check':self.checkers}.get(operation,{})
        adapter = adapters.get(binding.workspace_id)
        if adapter is None:
            raise Rejected('operation adapter is not available')
        # Do not accept a new model payload at the execution boundary.
        if sha256(call['payload_json'].encode('utf-8')).hexdigest() != call['payload_hash']:
            raise Rejected('saved operation payload changed')
        check_contract = None
        if operation == 'run_check':
            check_id = json.loads(call['payload_json'])['arguments']['check_id']
            check_contract = adapter.contract_hash(check_id)
            if check_contract is None or check_contract != call.get('check_contract'):
                raise Rejected('check has no matching reserved definition')
        if self._stopped(binding, may_continue):
            raise Rejected('host job no longer permits execution')
        started = self.reservations.begin(binding, call_id, check_contract=check_contract)
        if started['payload_hash'] != call['payload_hash']:
            raise Rejected('operation changed during claim')
        payload = json.loads(started['payload_json'])
        if payload['operation'] != operation:
            raise Rejected('saved operation differs from adapter')
        # Adapter failure deliberately leaves unknown; no automatic re-execution.
        if operation == 'read_files':
            receipt = {'operation': operation, 'files': adapter.read_files(**payload['arguments'])}
        elif operation == 'run_check':
            try:
                check = adapter.run_operation(self.operation_id(binding,call_id),started['payload_hash'],
                                          **payload['arguments'], expected_contract=check_contract,
                                          max_bytes=current['grant']['max_bytes'],
                                          timeout=current['grant']['deadline']-self.reservations.clock(),
                                          stopped=lambda:self._stopped(binding, may_continue))
            except CheckOutcomeUnknown as exc:
                self.reservations.observe(binding,call_id,started['payload_hash'],
                    {'at':self.reservations.clock(),'kind':'check_process_unknown',
                     'workspace_path':getattr(exc,'workspace_path',None),
                     'supervisor_pid':getattr(exc,'supervisor_pid',None)})
                raise
            receipt = {'operation':operation,'check':check}
        else:
            journaled = getattr(adapter, 'apply_operation', None)
            if callable(journaled):
                change = journaled(self.operation_id(binding,call_id), started['payload_hash'], payload['arguments'], current['grant']['max_bytes'])
            else:
                change = adapter.apply_change(**payload['arguments'], max_bytes=current['grant']['max_bytes'])
            receipt = {'operation': operation, 'change': change}
        self._stopped(binding, may_continue)
        return self.reservations.finish(binding, call_id, started['payload_hash'], receipt)

    def recover_check(self,binding,call_id):
        current=self.reservations.store.read(binding.grant_id)
        call=current['calls'].get(call_id)
        if (call is None or call['operation']!='run_check' or call.get('started_binding')!=asdict(binding)
            or call['state'] not in {'unknown','completed','quarantined'} or call.get('started_check_contract') is None):
            raise Rejected('check is not recoverable')
        if sha256(call['payload_json'].encode()).hexdigest()!=call['payload_hash']:
            raise Rejected('saved check payload changed')
        adapter=self.checkers.get(binding.workspace_id)
        if adapter is None:
            raise Rejected('check recovery adapter unavailable')
        saved=adapter.completion(self.operation_id(binding,call_id),call['payload_hash'],call['started_check_contract'])
        if saved['state']!='completed':
            return call
        if saved['receipt']['check_id']!=json.loads(call['payload_json'])['arguments']['check_id']:
            raise Rejected('recovered check identity changed')
        return self.reservations.finish(binding,call_id,call['payload_hash'],{'operation':'run_check','check':saved['receipt']})

    def retire_check(self,binding,call_id):
        call=self.recover_check(binding,call_id)
        if call['state'] not in {'completed','quarantined'}:
            raise Rejected('check outcome is still unknown')
        return self.checkers[binding.workspace_id].retire_operation(
            self.operation_id(binding,call_id),call['payload_hash'],call['started_check_contract'])

    def inspect_unknown_write(self, binding, call_id):
        from dataclasses import asdict
        current = self.reservations.store.read(binding.grant_id)
        call = current['calls'].get(call_id)
        if (call is None or call['state'] != 'unknown' or call['operation'] != 'apply_changes'
            or call.get('started_binding') != asdict(binding)):
            raise Rejected('not an owned unknown write')
        writer = self.writers.get(binding.workspace_id)
        if writer is None:
            raise Rejected('write inspection adapter unavailable')
        if sha256(call['payload_json'].encode()).hexdigest() != call['payload_hash']:
            raise Rejected('saved operation payload changed')
        args = json.loads(call['payload_json'])['arguments']
        observed = writer.inspect_file(args['path'],current['grant']['max_bytes'])
        desired = sha256(args['content'].encode()).hexdigest() if args['content'] is not None else None
        observation = {'at':self.reservations.clock(),'file':observed,
                       'desired_state_matches':observed['sha256']==desired,
                       'before_state_matches':observed['sha256']==args['before_hash'],
                       'operation_attribution_proven':False}
        return self.reservations.observe(binding,call_id,call['payload_hash'],observation)

    @staticmethod
    def operation_id(binding, call_id):
        return sha256(json.dumps({'binding':asdict(binding),'call_id':call_id},sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def recover_write(self, binding, call_id):
        current = self.reservations.store.read(binding.grant_id)
        call = current['calls'].get(call_id)
        if (call is None or call['operation'] != 'apply_changes' or call.get('started_binding') != asdict(binding)
            or call['state'] not in {'unknown','completed','quarantined'}):
            raise Rejected('write is not recoverable')
        if sha256(call['payload_json'].encode()).hexdigest() != call['payload_hash']:
            raise Rejected('saved write payload changed')
        adapter = self.writers.get(binding.workspace_id)
        recover = getattr(adapter,'completion',None)
        if not callable(recover): raise Rejected('durable completion unavailable')
        journal = recover(self.operation_id(binding,call_id),call['payload_hash'])
        if journal['state'] != 'completed': return call
        receipt = journal['receipt']
        observed = adapter.inspect_file(receipt['path'],current['grant']['max_bytes'])
        if observed['sha256'] != receipt['after_hash']:
            raise Rejected('completed write has since changed')
        return self.reservations.finish(binding,call_id,call['payload_hash'],{'operation':'apply_changes','change':receipt})
