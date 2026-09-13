"""Transitions to persist before Docker effects; never retry an uncertain start."""
from copy import deepcopy

from .admission import Rejected, content_hash


def transition(current, event, *, container_id=None, result=None):
    """Return a new record. The application must commit it before any effect.

    A missing create response is resolved by ownership inspection, not another
    create. A missing start response is resolved by inspection, never start again.
    Results are supplied only by the owned daemon adapter, not model output.
    """
    state=deepcopy(current)
    phase=state['phase']
    if event=='reserve_create' and phase=='claimed':
        state['phase']='creating'
    elif event=='created' and phase in ('creating','created'):
        content_hash(container_id)
        if phase=='created' and state['container_id']!=container_id:
            raise Rejected('container identity cannot change')
        state.update(phase='created',container_id=container_id)
    elif event=='reserve_start' and phase=='created':
        state['phase']='starting'
    elif event=='running' and phase in ('starting','running'):
        state['phase']='running'
    elif event=='reserve_stop' and phase in ('starting','running','stopping'):
        state['phase']='stopping'
    elif event=='exited' and phase in ('starting','running','stopping','exited'):
        if (type(result) is not dict or set(result)!={'exit_code','oom_killed'}
            or type(result['exit_code']) is not int or not 0<=result['exit_code']<=255
            or type(result['oom_killed']) is not bool):
            raise Rejected('invalid daemon exit observation')
        if phase=='exited' and state['result']!=result:
            raise Rejected('container result is immutable')
        state.update(phase='exited',result=deepcopy(result))
    elif event=='reserve_retire' and phase in ('created','exited','retiring'):
        state['phase']='retiring'
    elif event=='absent' and phase in ('retiring','retired'):
        state['phase']='retired'
    else:
        raise Rejected('container transition requires reconciliation')
    return state
