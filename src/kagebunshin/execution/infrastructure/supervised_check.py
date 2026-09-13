"""Host client for the private supervisor, with bounded I/O and shutdown."""
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import time

from ..domain.admission import Rejected
from ..domain.checks import CheckOutcomeUnknown
from .check_supervisor import supervisor_argv
from .check_journal import CheckJournal


def run_supervised_check(*, argv, cwd, env, timeout, max_output_bytes, stopped, journal_path=None, operation_id=None, journal_context=None):
    if (type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0 < timeout <= 300
        or type(max_output_bytes) is not int or not 0 < max_output_bytes <= 1_048_576):
        raise Rejected('invalid supervised check bounds')
    config = {'argv':argv,'cwd':str(cwd),'env':env,'timeout':timeout,'max_output_bytes':max_output_bytes}
    journal_config = config
    journal = None
    if journal_path is not None or operation_id is not None:
        if journal_path is None or operation_id is None:
            raise Rejected('check journal path and operation identity are both required')
        if not Path(journal_path).is_absolute() or Path(journal_path).resolve().is_relative_to(Path(cwd).resolve()):
            raise Rejected('check journal must be outside the disposable workspace')
        journal = CheckJournal(journal_path)
        config = {**config,'journal':{'path':str(journal.path),'operation_id':operation_id}}
    outgoing = json.dumps(config,ensure_ascii=False,separators=(',',':')).encode()+b'\n'
    if len(outgoing)>131_072:
        raise Rejected('supervisor configuration exceeds bound')
    if stopped():
        raise Rejected('check stopped before supervisor creation')
    if journal is not None:
        journal.claim(operation_id,journal_config,context=journal_context)
    process = subprocess.Popen(supervisor_argv(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,env={},close_fds=True,start_new_session=True)
    reply, diagnostic = bytearray(), bytearray()
    reply_limit = max_output_bytes*8+8192
    offset = 0
    closed = False
    stop_sent = False
    callback_error = None
    uncertain = None
    deadline = time.monotonic()+timeout+2
    shutdown_deadline = None

    def close_lease(selector):
        nonlocal closed, shutdown_deadline
        if not closed:
            try:
                selector.unregister(process.stdin)
            except KeyError:
                pass
            process.stdin.close()
            closed = True
            shutdown_deadline = time.monotonic()+6

    try:
        with selectors.DefaultSelector() as selector:
            for pipe,name,event in ((process.stdin,'input',selectors.EVENT_WRITE),
                                    (process.stdout,'output',selectors.EVENT_READ),
                                    (process.stderr,'error',selectors.EVENT_READ)):
                os.set_blocking(pipe.fileno(),False)
                selector.register(pipe,event,name)
            while selector.get_map() or process.poll() is None:
                if not closed and not stop_sent:
                    try:
                        requested = stopped()
                    except Exception as exc:
                        callback_error = exc
                        requested = True
                    if requested:
                        if offset == len(outgoing):
                            outgoing += b'STOP\n'
                            selector.register(process.stdin,selectors.EVENT_WRITE,'input')
                            stop_sent = True
                            shutdown_deadline = time.monotonic()+6
                        else:
                            close_lease(selector)
                now = time.monotonic()
                if now >= deadline and shutdown_deadline is None:
                    close_lease(selector)
                if shutdown_deadline is not None and now >= shutdown_deadline:
                    uncertain = 'supervisor did not confirm shutdown'
                    break
                for key,_ in selector.select(.05):
                    if key.data == 'input':
                        try:
                            offset += os.write(key.fileobj.fileno(),outgoing[offset:offset+4096])
                        except BrokenPipeError:
                            close_lease(selector)
                            continue
                        if offset == len(outgoing):
                            selector.unregister(key.fileobj)
                        continue
                    chunk = os.read(key.fileobj.fileno(),16_384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target,limit = (reply,reply_limit) if key.data == 'output' else (diagnostic,8192)
                    if len(target)+len(chunk)>limit:
                        uncertain = 'supervisor response exceeded bound'
                        close_lease(selector)
                    else:
                        target.extend(chunk)
                if process.poll() is not None and not any(k.data!='input' for k in selector.get_map().values()):
                    break
            if process.poll() is None:
                close_lease(selector)
                uncertain = uncertain or 'supervisor outcome unavailable'
        if uncertain or process.poll() is None or process.returncode != 0:
            raise CheckOutcomeUnknown(uncertain or 'supervisor failed before confirmed result')
        try:
            result = json.loads(reply)
            if (type(result) is not dict or type(result.get('returncode')) is not int
                or result.get('reason') not in {'completed','stopped','owner_lost','timeout','output_limit'}
                or type(result.get('output_bytes')) is not int or not 0 <= result['output_bytes'] <= max_output_bytes
                or not all(type(result.get(name)) is str for name in ('stdout','stderr','stdout_sha256','stderr_sha256'))):
                raise ValueError('invalid supervisor receipt')
        except (ValueError,TypeError) as exc:
            raise CheckOutcomeUnknown('supervisor result could not be validated') from exc
        if callback_error is not None:
            raise callback_error
        return result
    except BaseException as exc:
        if isinstance(exc,CheckOutcomeUnknown):
            exc.supervisor_pid = process.pid
        if process.poll() is None and not isinstance(exc,CheckOutcomeUnknown):
            unknown = CheckOutcomeUnknown('supervisor observation interrupted')
            unknown.supervisor_pid = process.pid
            raise unknown from exc
        raise
    finally:
        if not closed:
            process.stdin.close()
        # Do not kill the guardian before it can kill/reap its own child. A live
        # guardian leaves an unknown outcome and the workspace is retained.
        process.stdout.close()
        process.stderr.close()
