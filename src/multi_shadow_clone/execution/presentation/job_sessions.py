"""Host registry adapting orchestration's session port to execution grants."""
from hashlib import sha256
import json
from types import MappingProxyType
from copy import deepcopy

from ..domain.admission import Binding, Grant, Rejected
from ..domain.diagnostics import diagnostic
from .codex_tools import PreparedCodexTools
from ..application.executor import Executor


def fingerprint(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class JobSessions:
    def __init__(self, reservations, executor, workspace_contracts, *, attempt_checkers=None, leases=None):
        self.leases = leases
        self.attempt_checkers = MappingProxyType(dict(attempt_checkers or {}))
        self.reservations = reservations
        self.executor = executor
        self.workspace_contracts = MappingProxyType(dict(workspace_contracts))

    def scope_contract(self, scope):
        workspace = scope['workspace_id']
        registered = self.workspace_contracts.get(workspace)
        if registered is None:
            raise Rejected('workspace is not host registered')
        current = registered()
        if not set(scope['paths']) <= set(current['paths']):
            raise Rejected('scope exceeds registered paths')
        adapters = {'read_files': self.executor.readers, 'apply_changes': self.executor.writers,
                    'run_check': self.executor.checkers}
        for operation in scope['operations']:
            if workspace not in adapters[operation] and not (operation == 'run_check' and workspace in self.attempt_checkers):
                raise Rejected('required workspace adapter missing')
        registry = self.attempt_checkers.get(workspace)
        if registry and {'apply_changes', 'run_check'} <= set(scope['operations']):
            raise Rejected('template checks require a separate stage after writes')
        if registry and 'apply_changes' in scope['operations'] and set(scope['paths']) & registry.protected_paths():
            raise Rejected('fixed host tests cannot be model write targets')
        checks = {}
        for check in scope['checks']:
            if registry is not None:
                checks[check] = registry.contract(check)
                continue
            value = self.executor.checkers[workspace].contract_hash(check)
            if value is None:
                raise Rejected('registered check unavailable')
            checks[check] = value
        return {'workspace': current, 'checks': checks}

    def contract(self, policy):
        contracts = []
        for scope in policy['scopes']:
            current = self.scope_contract(scope)
            if fingerprint(current) != scope['runtime_hash']:
                raise Rejected('workspace runtime changed')
            contracts.append({'node_id': scope['node_id'], 'stage': scope['stage'], 'runtime': current})
        return {'kind': 'host-job-sessions-v1', 'scopes': contracts}

    def prepare_policy(self, draft):
        prepared=deepcopy(draft)
        for scope in prepared['scopes']:
            scope['runtime_hash']=fingerprint(self.scope_contract(scope))
        return prepared

    def prepare(self, scope, request, deadline, stop_epoch):
        if (scope['node_id'], scope['stage'], scope['role_id']) != (request.node_id, request.stage, request.role_id):
            raise Rejected('scope differs from saved attempt')
        current = self.scope_contract(scope)
        if fingerprint(current) != scope['runtime_hash'] or request.may_continue is None or not request.may_continue():
            raise Rejected('runtime changed or job stopped')
        may_continue = request.may_continue
        if self.leases is not None:
            identity = {key: current['workspace'][key] for key in ('device', 'inode')}
            lease = self.leases.claim(identity, request.run_id, fingerprint(current['workspace']))
            def may_continue():
                return request.may_continue() and self.leases.owns(identity, lease)
        binding = Binding(request.attempt_id, request.run_id, request.node_id, request.attempt_id,
                          'pending', 'pending', stop_epoch, scope['workspace_id'], request.binding_hash)
        grant = Grant(binding, frozenset(scope['operations']), frozenset(scope['paths']),
                      frozenset(scope['checks']), scope['max_calls'], deadline, scope['max_bytes'])
        executor, check_contracts = self.executor, current['checks']
        registry = self.attempt_checkers.get(scope['workspace_id'])
        if registry is not None and scope['checks']:
            checker = registry.prepare(scope['checks'], request.attempt_id, request.binding_hash)
            check_contracts = {key: checker.contract_hash(key) for key in scope['checks']}
            executor = Executor(self.reservations, self.executor.readers, writers=self.executor.writers,
                                checkers={**self.executor.checkers, scope['workspace_id']: checker})
        return PreparedCodexTools(grant, self.reservations, executor,
                                  check_contracts=check_contracts, may_continue=may_continue)

    def outcome(self, run_id, attempt):
        saved = self.reservations.store.read(attempt['id'])
        binding = saved['grant']['binding']
        expected = {'run_id': run_id, 'attempt_id': attempt['id'], 'node_id': attempt['node_id'],
                    'thread_id': attempt['thread_id'], 'turn_id': attempt['turn_id'],
                    'contract_hash': attempt['binding_hash']}
        if any(binding[key] != value for key, value in expected.items()):
            raise Rejected('operation record differs from saved provider attempt')
        calls = saved['calls']
        complete = all(call['state'] == 'completed' for call in calls.values()) and not saved['stopped']
        passed = complete and all(call['operation'] != 'run_check'
            or call.get('receipt', {}).get('check', {}).get('passed') is True for call in calls.values())
        passed_checks = sorted({call['receipt']['check']['check_id'] for call in calls.values()
            if call['state'] == 'completed' and call['operation'] == 'run_check'
            and call.get('receipt', {}).get('check', {}).get('passed') is True})
        diagnostics, remaining = {}, 16384
        for key, call in sorted(calls.items()):
            diagnostics[key], remaining = diagnostic(call, remaining)
        return {'complete': complete, 'passed': passed, 'passed_checks': passed_checks,
                'calls': {key: {'operation': call['operation'], 'state': call['state'],
                               'diagnostic': diagnostics[key],
                               'payload_hash': call['payload_hash'],
                               'receipt_hash': fingerprint(call.get('receipt'))}
                          for key, call in sorted(calls.items())}}

    def recover_checks(self, run_id, attempt):
        """Host-only result recovery. Never invokes a model or run_operation."""
        self.outcome(run_id, attempt)
        saved = self.reservations.store.read(attempt['id'])
        binding = Binding(**saved['grant']['binding'])
        # A STOP changes the grant epoch; started_binding keeps the effect identity.
        checks = dict(self.executor.checkers)
        registry = self.attempt_checkers.get(binding.workspace_id)
        if registry is not None:
            checks[binding.workspace_id] = registry.restore(attempt['id'], attempt['binding_hash'])
        executor = Executor(self.reservations, self.executor.readers,
                            writers=self.executor.writers, checkers=checks)
        for call_id, call in saved['calls'].items():
            if call['operation'] != 'run_check' or call['state'] not in {'unknown', 'completed', 'quarantined'}:
                continue
            started = Binding(**call['started_binding'])
            executor.recover_check(started, call_id)
        return self.outcome(run_id, attempt)

    def retire(self, run):
        from ..domain.retirement import validate_retirement
        if (run.get('stopped') is not True or run.get('workspace_retirement',{}).get('state')!='retiring'
            or any(n.get('active') is not None for n in run['nodes'].values())
            or any(a['state']!='terminal' for a in run['attempts'])):
            raise Rejected('job retirement intent is not safely fenced')
        grants, proofs = {}, {}
        scopes = run.get('execution_policy',{}).get('scopes',[])
        for attempt in run['attempts']:
            scope=next((s for s in scopes if (s['node_id'],s['stage'])==(attempt['node_id'],attempt['stage'])),None)
            if scope is None:continue
            try:
                self.outcome(run['id'],attempt)
            except KeyError:
                if attempt.get('terminal_status')=='not_sent' and attempt.get('turn_id') is None:continue
                raise
            saved=self.reservations.stop(attempt['id'])
            grants[attempt['id']]=saved
            checks=dict(self.executor.checkers)
            registry=self.attempt_checkers.get(scope['workspace_id'])
            if registry and any(c['operation']=='run_check' and c['state']!='cancelled' for c in saved['calls'].values()):
                checks[scope['workspace_id']]=registry.restore(attempt['id'],attempt['binding_hash'])
            executor=Executor(self.reservations,self.executor.readers,writers=self.executor.writers,checkers=checks)
            for call_id,call in saved['calls'].items():
                if call['operation']=='run_check' and call['state'] in {'completed','quarantined'}:
                    retired=executor.retire_check(Binding(**call['started_binding']),call_id)
                    if retired.get('state') not in {'removed','path_absent'}:raise Rejected('check retirement not confirmed')
                    proofs[(attempt['id'],call_id)]={'retired':True,'snapshot':retired}
        evidence=validate_retirement(run,grants,proofs)
        evidence['grant_hashes']={key:fingerprint(value) for key,value in grants.items()}
        retirement_hash=fingerprint(evidence)
        released={}
        if self.leases is not None:
            for workspace in sorted({s['workspace_id'] for s in scopes}):
                contract=self.workspace_contracts[workspace]()
                identity={key:contract[key] for key in ('device','inode')}
                lease=self.leases.read(identity,run['id'])
                if lease is None or lease['run_id']!=run['id']:
                    released[workspace]={'state':'not_owned'}
                    continue
                if lease['contract_hash']!=fingerprint(contract):raise Rejected('leased workspace contract changed')
                if lease['state']=='released':
                    if lease['retirement_hash']!=retirement_hash:raise Rejected('retirement evidence changed')
                    released[workspace]=lease
                else:released[workspace]=self.leases.release(identity,lease,retirement_hash)
        return {'evidence':evidence,'retirement_hash':retirement_hash,'leases':released}
