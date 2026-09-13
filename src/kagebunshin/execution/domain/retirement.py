"""Evidence requirements for releasing a job's workspace, without performing I/O."""
from .admission import Rejected


def validate_retirement(run, grants, retired_checks):
    if run.get('stopped') is not True:
        raise Rejected('job must be fenced before workspace retirement')
    if any(node.get('active') is not None for node in run['nodes'].values()):
        raise Rejected('job has an active or unknown reservation')
    attempts={attempt['id']:attempt for attempt in run['attempts']}
    if any(attempt.get('state')!='terminal' for attempt in attempts.values()):
        raise Rejected('provider attempts are unresolved')
    if set(grants)-set(attempts):
        raise Rejected('unexpected operation grant')
    scoped={(s['node_id'],s['stage']) for s in run.get('execution_policy',{}).get('scopes',[])}
    for attempt_id,attempt in attempts.items():
        if (attempt['node_id'],attempt['stage']) not in scoped:
            continue
        grant=grants.get(attempt_id)
        if grant is None:
            if attempt.get('terminal_status')!='not_sent' or attempt.get('turn_id') is not None:
                raise Rejected('missing operation history for dispatched attempt')
            continue
        binding=grant['grant']['binding']
        if any(binding.get(key)!=value for key,value in {
            'run_id':run['id'],'attempt_id':attempt_id,'node_id':attempt['node_id'],
            'contract_hash':attempt['binding_hash'],'thread_id':attempt.get('thread_id'),
            'turn_id':attempt.get('turn_id')}.items()):
            raise Rejected('operation history differs from job')
        if grant.get('stopped') is not True:
            raise Rejected('operation grant must be fenced')
        for call_id,call in grant['calls'].items():
            if call.get('state') not in {'completed','quarantined','cancelled'}:
                raise Rejected('operation outcome is unresolved')
            if call['operation']=='run_check' and call['state']!='cancelled':
                proof=retired_checks.get((attempt_id,call_id))
                if not proof or proof.get('retired') is not True:
                    raise Rejected('check resources lack retirement evidence')
    return {'run_id':run['id'],'stop_epoch':run['stop_epoch'],
            'attempt_ids':sorted(attempts),'grant_ids':sorted(grants),
            'check_ids':[list(key) for key in sorted(retired_checks)]}
