"""Bounded lifetime for a host-constructed check command.

This internal primitive is not a model tool. The caller must supply an isolated
command and disposable workspace. Parent-death recovery is not provided here.
"""
from hashlib import sha256
import math
import os
import selectors
import subprocess
import time

from ..domain.admission import Rejected


def run_check_process(*, argv, cwd, env, timeout, max_output_bytes, stopped):
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
        or not math.isfinite(timeout) or not 0 < timeout <= 300
        or type(max_output_bytes) is not int or not 0 < max_output_bytes <= 1_048_576):
        raise Rejected('invalid check process bounds')
    if (type(argv) not in (list, tuple) or not argv
        or not all(type(arg) is str and '\x00' not in arg for arg in argv)
        or not os.path.isabs(argv[0])):
        raise Rejected('invalid host check command')
    if stopped():
        raise Rejected('check stopped before process creation')
    deadline = time.monotonic() + timeout
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    reason = 'completed'
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               close_fds=True, start_new_session=True, shell=False)
    try:
        with selectors.DefaultSelector() as selector:
            for name, pipe in (('stdout', process.stdout), ('stderr', process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                if stopped():
                    reason = 'stopped'
                    break
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    reason = 'timeout'
                    break
                for key, _ in selector.select(min(.05, remaining_time)):
                    chunk = os.read(key.fileobj.fileno(), 16_384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining_bytes = max_output_bytes - sum(map(len, output.values()))
                    output[key.data].extend(chunk[:remaining_bytes])
                    if len(chunk) > remaining_bytes:
                        reason = 'output_limit'
                        break
                if reason != 'completed':
                    break
        if reason != 'completed' and process.poll() is None:
            process.kill()
        returncode = process.wait(timeout=5)
        return {'returncode': returncode, 'reason': reason,
                'output_bytes': sum(map(len, output.values())),
                **{name: bytes(data).decode('utf-8', errors='replace') for name, data in output.items()},
                **{name + '_sha256': sha256(data).hexdigest() for name, data in output.items()}}
    finally:
        # Callback errors and observation failures must not orphan our child.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
