"""Container effects follow durable reservations; recovery never starts work."""
from ..domain.admission import Rejected


class Containers:
    def __init__(self,journal,runtime):
        self.journal=journal
        self.runtime=runtime

    def prepare(self,operation_id,*,context=None):
        config=self.runtime.contract()
        self.journal.claim(operation_id,config,context=context)
        self.journal.advance(operation_id,config,'reserve_create')
        container_id=self.runtime.create()
        observed=self.runtime.inspect()
        if observed is None or observed['Id']!=container_id:
            raise Rejected('created container requires reconciliation')
        self.runtime.verify_created(observed)
        return self.journal.advance(operation_id,config,'created',container_id=container_id)

    def recover_created(self,operation_id):
        config=self.runtime.contract()
        current=self.journal.read(operation_id)
        if current is None or current['config']!=config or current['phase'] not in ('creating','created'):
            raise Rejected('container creation cannot be reconciled here')
        observed=self.runtime.inspect()
        if observed is None:
            raise Rejected('creation outcome remains unknown; do not recreate')
        self.runtime.verify_created(observed)
        return self.journal.advance(operation_id,config,'created',container_id=observed['Id'])

    def retire(self,operation_id):
        config=self.runtime.contract()
        current=self.journal.read(operation_id)
        if current is None or current['config']!=config:
            raise Rejected('container retirement differs from claim')
        if current['phase']=='retired':
            return current
        # Persist permission to remove before invoking the daemon.
        current=self.journal.advance(operation_id,config,'reserve_retire')
        observed=self.runtime.inspect()
        if observed is not None:
            if observed['Id']!=current['container_id']:
                raise Rejected('container was replaced')
            self.runtime.verify_owned(observed)
            if observed['State']['Status'] not in ('created','exited') or observed['State']['Running']:
                raise Rejected('container has not stopped')
            self.runtime.remove(current['container_id'])
        if self.runtime.inspect() is not None:
            raise Rejected('container retirement is not confirmed')
        return self.journal.advance(operation_id,config,'absent')

    def _owned(self,operation_id):
        config=self.runtime.contract()
        current=self.journal.read(operation_id)
        if current is None or current['config']!=config:
            raise Rejected('container execution differs from claim')
        observed=self.runtime.inspect()
        if observed is None or observed['Id']!=current.get('container_id'):
            raise Rejected('container execution identity is unknown')
        self.runtime.verify_owned(observed)
        return config,current,observed

    def start(self,operation_id):
        config,current,observed=self._owned(operation_id)
        self.runtime.verify_created(observed)
        self.journal.advance(operation_id,config,'reserve_start')
        self.runtime.start(current['container_id'])
        return self.reconcile_execution(operation_id)

    def reconcile_execution(self,operation_id):
        config,current,observed=self._owned(operation_id)
        if current['phase'] not in ('starting','running','stopping','exited'):
            raise Rejected('container is not awaiting execution reconciliation')
        state=observed['State']
        if state.get('Status')=='exited' and state.get('Running') is False and state.get('Pid')==0:
            return self.journal.advance(operation_id,config,'exited',result={
                'exit_code':state['ExitCode'],'oom_killed':state['OOMKilled']})
        if state.get('Status')=='running' and state.get('Running') is True:
            if current['phase']=='stopping':
                return current
            return self.journal.advance(operation_id,config,'running')
        raise Rejected('container start outcome remains unknown')

    def stop(self,operation_id):
        config,current,observed=self._owned(operation_id)
        if current['phase']=='exited':
            return self.reconcile_execution(operation_id)
        self.journal.advance(operation_id,config,'reserve_stop')
        if observed['State'].get('Running') is True:
            self.runtime.kill(current['container_id'])
        return self.reconcile_execution(operation_id)

    def execute_attached(self,operation_id,*,timeout,max_output_bytes,stopped):
        started=False
        try:
            with self.runtime.execution_scope(timeout,stopped):
                config,current,observed=self._owned(operation_id)
                self.runtime.verify_created(observed)
                if stopped():
                    raise Rejected('container execution stopped before reservation')
                self.journal.reserve_attached(operation_id,config,timeout=timeout,max_output_bytes=max_output_bytes)
                started=True
                result=self.runtime.start_attached(current['container_id'],timeout=timeout,
                    max_output_bytes=max_output_bytes,stopped=stopped)
            self.journal.record_output(operation_id,config,result)
            state=self.reconcile_execution(operation_id)
            if state['phase']!='exited':
                state=self.stop(operation_id)
            return state
        except BaseException:
            # CLI termination is not daemon termination. Preserve unknown if
            # the daemon cannot be reached; never recreate or restart here.
            if started:
                self.stop(operation_id)
            raise
