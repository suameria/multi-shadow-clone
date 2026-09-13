"""Reserve complete batches; never execute effects during a transaction."""
from dataclasses import asdict
import json
from ..domain.admission import Binding, Grant, Rejected, admit_batch, validate_grant, content_hash
from ..ports import OperationStore


def decode_grant(raw):
    return Grant(Binding(**raw['binding']), frozenset(raw['operations']), frozenset(raw['paths']),
                 frozenset(raw['checks']), raw['remaining'], raw['deadline'], raw['max_bytes'])


class Reservations:
    def __init__(self, store: OperationStore, clock):
        self.store, self.clock = store, clock

    def register(self, grant, *, check_contracts=None):
        validate_grant(grant, self.clock())
        contracts = {} if check_contracts is None else dict(check_contracts)
        if check_contracts is not None and set(contracts) != set(grant.checks):
            raise Rejected('registered check contracts must match the grant')
        for digest in contracts.values():
            content_hash(digest)
        raw = asdict(grant)
        for field in ('operations', 'paths', 'checks'):
            raw[field] = sorted(raw[field])
        def create(current):
            if current is not None:
                raise Rejected('grant already registered')
            return {'grant': raw, 'stopped': False, 'calls': {}, 'check_contracts': contracts}
        return self.store.transact(grant.binding.grant_id, create)

    def reserve(self, binding, proposals):
        def reserve(current):
            if current is None:
                raise Rejected('unknown grant')
            admitted = admit_batch(decode_grant(current['grant']), binding, proposals,
                                   {k: v['payload_hash'] for k, v in current['calls'].items()},
                                   now=self.clock(), stopped=current['stopped'])
            for call in admitted:
                if not call.replay:
                    current['calls'][call.call_id] = {'operation': call.operation,
                        'payload_json': call.payload_json, 'payload_hash': call.payload_hash, 'state': 'reserved'}
                    if call.operation == 'run_check':
                        check_id = json.loads(call.payload_json)['arguments']['check_id']
                        current['calls'][call.call_id]['check_contract'] = current.get('check_contracts', {}).get(check_id)
                    current['grant']['remaining'] -= 1
            return current
        return self.store.transact(binding.grant_id, reserve)

    def stop(self, grant_id):
        def stop(current):
            if current is None:
                raise Rejected('unknown grant')
            if not current['stopped']:
                current['stopped'] = True
                current['grant']['binding']['stop_epoch'] += 1
                # Serialized with begin: only operations never handed to an
                # adapter can be cancelled and release their reservation.
                for call in current['calls'].values():
                    if call['state'] == 'reserved':
                        call['state'] = 'cancelled'
                        current['grant']['remaining'] += 1
            return current
        return self.store.transact(grant_id, stop)

    def begin(self, binding, call_id, *, check_contract=None):
        """Commit an uncertain outcome before handing the operation to an adapter.

        Only the successful caller may execute. Reopening a store or seeing an
        unknown call is not permission to execute it again.
        """
        def begin(current):
            if current is None:
                raise Rejected('unknown grant')
            grant = decode_grant(current['grant'])
            if current['stopped'] or grant.binding != binding or not self.clock() < grant.deadline:
                raise Rejected('execution binding expired or stopped')
            call = current['calls'].get(call_id)
            if call is None or call['state'] != 'reserved':
                raise Rejected('operation is not awaiting execution')
            try:
                payload = json.loads(call['payload_json'])
            except (TypeError, ValueError) as exc:
                raise Rejected('saved operation is not valid JSON') from exc
            if type(payload) is not dict or set(payload) != {'operation', 'arguments'} or payload['operation'] != call['operation']:
                raise Rejected('saved operation structure changed')
            admit_batch(grant, binding, [{'call_id': call_id, **payload}],
                        {call_id: call['payload_hash']}, now=self.clock(), stopped=current['stopped'])
            if check_contract is not None:
                content_hash(check_contract)
                if call['operation'] != 'run_check' or call.get('check_contract') != check_contract:
                    raise Rejected('reserved check definition differs from the adapter')
                call['started_check_contract'] = check_contract
            elif call.get('check_contract') is not None:
                raise Rejected('registered check requires its exact contract at begin')
            call.update(state='unknown', started_binding=asdict(binding))
            return current
        return self.store.transact(binding.grant_id, begin)['calls'][call_id]

    def execution_stopped(self, binding):
        current = self.store.read(binding.grant_id)
        grant = decode_grant(current['grant'])
        return current['stopped'] or grant.binding != binding or not self.clock() < grant.deadline

    def finish(self, binding, call_id, payload_hash, receipt):
        """Record a host adapter's known outcome, preserving late results as history."""
        def finish(current):
            if current is None:
                raise Rejected('unknown grant')
            call = current['calls'].get(call_id)
            if (call is None or call.get('started_binding') != asdict(binding)
                or call['payload_hash'] != payload_hash):
                raise Rejected('result does not belong to the started operation')
            if call.get('started_check_contract') is not None:
                if (type(receipt) is not dict or receipt.get('operation') != 'run_check'
                    or type(receipt.get('check')) is not dict
                    or receipt['check'].get('definition_hash') != call['started_check_contract']):
                    raise Rejected('check receipt does not match the started definition')
            if call['state'] in {'completed', 'quarantined'}:
                if call.get('receipt') != receipt:
                    raise Rejected('conflicting operation receipt')
                return current
            if call['state'] != 'unknown':
                raise Rejected('operation was not started')
            current_binding = decode_grant(current['grant']).binding
            late = current['stopped'] or current_binding != binding or not self.clock() < current['grant']['deadline']
            call.update(state='quarantined' if late else 'completed', receipt=receipt)
            return current
        return self.store.transact(binding.grant_id, finish)['calls'][call_id]

    def observe(self, binding, call_id, payload_hash, observation):
        """Keep bounded observations without releasing unknown reservations."""
        def observe(current):
            if current is None:
                raise Rejected('unknown grant')
            call = current['calls'].get(call_id)
            if (call is None or call['state'] != 'unknown' or call.get('started_binding') != asdict(binding)
                or call['payload_hash'] != payload_hash):
                raise Rejected('observation does not belong to an unknown operation')
            call['latest_observation'] = observation
            return current
        return self.store.transact(binding.grant_id,observe)['calls'][call_id]
