import errno
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from kagebunshin.execution.infrastructure.macos_sandbox import python_check_policy
from kagebunshin.execution.infrastructure.check_process import run_check_process


@unittest.skipUnless(sys.platform == 'darwin', 'macOS Seatbelt integration')
class MacOSCheckPolicyTest(unittest.TestCase):
    def test_real_check_cannot_reach_other_files_network_or_child_processes(self):
        runtime = Path(sys.base_prefix).resolve()
        executable = runtime/'Resources/Python.app/Contents/MacOS/Python'
        if not executable.is_file():
            self.skipTest('requires the explicitly tested macOS framework runtime')
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            root = Path(directory).resolve()
            workspace = root/'workspace'
            workspace.mkdir()
            scratch = workspace/'scratch'
            scratch.mkdir()
            secret = root/'secret'
            secret.write_text('test-only-canary')
            (workspace/'escape').symlink_to(secret)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(.05)
            script = workspace/'check.py'
            script.write_text('''import os, socket, subprocess, json, sys
from pathlib import Path
result = {}
Path('scratch/allowed').write_text('ok')
result['workspace'] = Path('scratch/allowed').read_text()
def connect():
    with socket.socket() as connection:
        connection.settimeout(1)
        connection.connect(('127.0.0.1', int(sys.argv[2])))
for name, action in [
    ('secret', lambda: Path(sys.argv[1]).read_text()),
    ('symlink', lambda: Path('escape').read_text()),
    ('outside_write', lambda: Path(sys.argv[1]+'-write').write_text('bad')),
    ('source_write', lambda: Path('check.py').write_text('bad')),
    ('network', connect), ('fork', os.fork),
    ('spawn', lambda: subprocess.run(['/usr/bin/true'], check=True))]:
    try:
        value = action()
        if name == 'fork' and value == 0: os._exit(0)
        result[name] = 'ALLOWED'
    except OSError as exc: result[name] = exc.errno
print(json.dumps(result))
''')
            original = script.read_bytes()
            policy = python_check_policy(workspace=workspace, scratch=scratch,
                                         runtime=runtime, executable=executable)
            result = run_check_process(argv=['/usr/bin/sandbox-exec', '-p', policy, str(executable),
                                     '-I', '-S', '-B', str(script), str(secret), str(listener.getsockname()[1])],
                                    cwd=workspace, env={'PATH':'/usr/bin:/bin', 'HOME':str(scratch), 'TMPDIR':str(scratch)},
                                    max_output_bytes=8192, timeout=15, stopped=lambda:False)
            self.assertEqual(result['reason'], 'completed')
            self.assertEqual(result['returncode'], 0, result['stderr'])
            observed = json.loads(result['stdout'])
            self.assertEqual(observed.pop('workspace'), 'ok')
            self.assertEqual(set(observed), {'secret','symlink','outside_write','source_write','network','fork','spawn'})
            self.assertEqual(set(observed.values()), {errno.EPERM})
            self.assertFalse(Path(str(secret)+'-write').exists())
            self.assertEqual(script.read_bytes(), original)
            self.assertEqual(secret.read_text(), 'test-only-canary')
            with self.assertRaises(TimeoutError):
                connection, _ = listener.accept()
                connection.close()
