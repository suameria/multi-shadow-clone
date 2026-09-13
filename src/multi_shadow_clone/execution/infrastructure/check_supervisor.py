"""Private supervisor protocol: a live input pipe is the caller's lease.

Only host code may launch this process and construct its configuration. This is
not an agent-facing command interface. No child inherits the lease descriptor.
"""
import json
import os
from pathlib import Path
import selectors
import sys
import time

from ..domain.admission import Rejected
from .check_process import run_check_process
from .check_journal import CheckJournal


def supervisor_argv():
    bootstrap = ('import sys;sys.path.insert(0,sys.argv[1]);'
                 'from multi_shadow_clone.execution.infrastructure.check_supervisor import main;main()')
    return [sys.executable, '-I', '-S', '-c', bootstrap, str(Path(__file__).resolve().parents[3])]


def _configuration(fd, allowed_keys=None):
    deadline = time.monotonic() + 10
    buffer = bytearray()
    os.set_blocking(fd, False)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if not selector.select(min(.1, max(0, deadline-time.monotonic()))):
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                raise Rejected('owner disconnected before supervisor configuration')
            buffer.extend(chunk)
            if len(buffer) > 131_072:
                raise Rejected('supervisor configuration exceeds bound')
            if b'\n' in buffer:
                line, pending = bytes(buffer).split(b'\n', 1)
                config = json.loads(line)
                required = {'argv','cwd','env','timeout','max_output_bytes'}
                allowed = allowed_keys if allowed_keys is not None else (required,required|{'journal'})
                if type(config) is not dict or set(config) not in allowed:
                    raise Rejected('invalid supervisor configuration')
                return config, pending
    raise Rejected('supervisor configuration timed out')


class OwnerLease:
    def __init__(self, fd, pending=b''):
        self.fd, self.pending = fd, pending
        self.reason = None
        os.set_blocking(fd, False)

    def stopped(self):
        if self.reason is not None:
            return True
        if self.pending:
            self.reason = 'stopped'
            return True
        try:
            command = os.read(self.fd, 4096)
        except BlockingIOError:
            return False
        self.reason = 'owner_lost' if command == b'' else 'stopped'
        return True


def main():
    config, pending = _configuration(sys.stdin.fileno())
    journal_spec = config.pop('journal',None)
    journal = None
    if journal_spec is not None:
        if type(journal_spec) is not dict or set(journal_spec)!={'path','operation_id'}:
            raise Rejected('invalid supervisor journal binding')
        journal = CheckJournal(journal_spec['path'])
        journal.start(journal_spec['operation_id'],config,os.getpid())
    lease = OwnerLease(sys.stdin.fileno(), pending)
    result = run_check_process(**config, stopped=lease.stopped)
    if result['reason'] == 'stopped' and lease.reason == 'owner_lost':
        result['reason'] = 'owner_lost'
    if journal is not None:
        journal.complete(journal_spec['operation_id'],config,result)
    # The caller may be dead; cleanup has already completed even if this write
    # raises BrokenPipeError. When configured, the durable result is already saved.
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')), flush=True)
