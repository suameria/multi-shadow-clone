"""Private Docker guardian. Bootstrap supplies the concrete service."""
import json
import os
from pathlib import Path
import sys
import time

from .check_supervisor import _configuration, OwnerLease


def supervisor_argv():
    code=('import sys;sys.path.insert(0,sys.argv[1]);'
          'from kagebunshin.bootstrap import container_supervisor_main;container_supervisor_main()')
    return [sys.executable,'-I','-S','-c',code,str(Path(__file__).resolve().parents[3])]


def serve(build):
    required={'runtime','journal_path','operation_id','timeout','max_output_bytes'}
    config,pending=_configuration(sys.stdin.fileno(),(required,required|{'deadline'}))
    service=build(config['runtime'],config['journal_path'])
    operation_id=config['operation_id']
    contract=service.runtime.contract()
    pid=os.getpid()
    lease=OwnerLease(sys.stdin.fileno(),pending)
    service.journal.claim_supervisor(operation_id,contract,pid)
    remaining=min(config['timeout'],config.get('deadline',time.monotonic()+config['timeout'])-time.monotonic())
    service.execute_attached(operation_id,timeout=remaining,
        max_output_bytes=config['max_output_bytes'],stopped=lease.stopped)
    reason=lease.reason or 'completed'
    if 'deadline' in config and time.monotonic()>=config['deadline']:
        reason='deadline'
    state=service.journal.complete_supervisor(operation_id,contract,pid,reason)
    # The result is durable before a dead caller's output pipe is touched.
    print(json.dumps(state,separators=(',',':')),flush=True)
