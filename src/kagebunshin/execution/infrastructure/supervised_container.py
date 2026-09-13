"""Bounded host client; results come from the durable journal, not stdout."""
import json
import math
import os
import subprocess
import time

from ..domain.admission import Rejected
from ..domain.checks import CheckOutcomeUnknown
from .container_journal import ContainerJournal
from .container_supervisor import supervisor_argv


def run_supervised_container(*,runtime,journal_path,operation_id,timeout,max_output_bytes,stopped):
    if (type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=300
        or type(max_output_bytes) is not int or not 0<max_output_bytes<=1048576):
        raise Rejected('invalid container supervisor bounds')
    deadline=time.monotonic()+timeout
    journal=ContainerJournal(journal_path)
    current=journal.read(operation_id)
    if (current is None or current['config']!=runtime or current['phase']!='created'
        or 'supervisor' in current):
        raise Rejected('container supervision requires an unclaimed created container')
    config={'runtime':runtime,'journal_path':str(journal.path),'operation_id':operation_id,
            'timeout':timeout,'deadline':deadline,'max_output_bytes':max_output_bytes}
    outgoing=json.dumps(config,separators=(',',':'),allow_nan=False).encode()+b'\n'
    if len(outgoing)>131072 or stopped():
        raise Rejected('container supervisor configuration stopped or oversized')
    process=subprocess.Popen(supervisor_argv(),stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,env={},close_fds=True,start_new_session=True)
    os.set_blocking(process.stdin.fileno(),False)
    offset=0
    closed=False
    stop_sent=False
    try:
        while process.poll() is None:
            now=time.monotonic()
            if not closed:
                if now>=deadline:
                    process.stdin.close()
                    closed=True
                elif not stop_sent and stopped():
                    if offset==len(outgoing):
                        outgoing+=b'STOP\n'
                        stop_sent=True
                    else:
                        process.stdin.close()
                        closed=True
                if not closed and offset<len(outgoing):
                    try:
                        offset+=os.write(process.stdin.fileno(),outgoing[offset:offset+4096])
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        process.stdin.close()
                        closed=True
            if now>=deadline+90:
                raise CheckOutcomeUnknown('container guardian shutdown not confirmed')
            time.sleep(.05)
        state=journal.read(operation_id)
        if (process.returncode!=0 or state['config']!=runtime or state['phase']!='exited'
            or state.get('supervisor',{}).get('pid')!=process.pid
            or state['supervisor'].get('reason') not in ('completed','stopped','owner_lost','deadline')):
            raise CheckOutcomeUnknown('container guardian has no confirmed durable completion')
        return state
    except BaseException as exc:
        if isinstance(exc,CheckOutcomeUnknown):
            exc.supervisor_pid=process.pid
            raise
        unknown=CheckOutcomeUnknown('container guardian observation interrupted')
        unknown.supervisor_pid=process.pid
        raise unknown from exc
    finally:
        if not closed:
            process.stdin.close()
        # Never kill the guardian while it may still be stopping the daemon.
