import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from multi_shadow_clone.execution.infrastructure.check_supervisor import supervisor_argv
from multi_shadow_clone.execution.infrastructure.macos_sandbox import python_check_policy
from multi_shadow_clone.execution.infrastructure.check_journal import CheckJournal


@unittest.skipUnless(sys.platform == 'darwin', 'macOS isolated supervisor integration')
class CheckSupervisorTest(unittest.TestCase):
    def test_sigkill_of_owner_terminates_and_reaps_isolated_check(self):
        runtime = Path(sys.base_prefix).resolve()
        executable = runtime/'Resources/Python.app/Contents/MacOS/Python'
        if not executable.is_file():
            self.skipTest('requires explicitly tested framework runtime')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root/'workspace'
            workspace.mkdir()
            scratch = workspace/'scratch'
            scratch.mkdir()
            marker = scratch/'child-pid'
            policy = python_check_policy(workspace=workspace,scratch=scratch,runtime=runtime,executable=executable)
            script = "import os\nfrom pathlib import Path\nPath('scratch/child-pid').write_text(str(os.getpid()))\nwhile True: pass"
            config = {'argv':['/usr/bin/sandbox-exec','-p',policy,str(executable),'-I','-S','-c',script],
                      'cwd':str(workspace),'env':{},'timeout':5,'max_output_bytes':1024}
            journal_path = root/'check-journal.sqlite3'
            journal = CheckJournal(journal_path)
            journal.claim('a'*64,config)
            config = {**config,'journal':{'path':str(journal_path),'operation_id':'a'*64}}
            config_path = root/'config.json'
            config_path.write_text(json.dumps({'command':supervisor_argv(),'config':config}))
            owner_script = '''import json,sys,subprocess,time
from pathlib import Path
root=Path(sys.argv[1]); config=json.loads((root/'config.json').read_text())
with (root/'result.json').open('wb') as output, (root/'supervisor-error').open('wb') as error:
    child=subprocess.Popen(config['command'],stdin=subprocess.PIPE,stdout=output,stderr=error,close_fds=True,start_new_session=True)
    child.stdin.write(json.dumps(config['config']).encode()+b'\\n');child.stdin.flush()
    print(child.pid,flush=True)
    time.sleep(30)
'''
            owner = subprocess.Popen([sys.executable,'-I','-S','-c',owner_script,str(root)],
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={})
            supervisor_pid = None
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(owner.stdout,selectors.EVENT_READ)
                    self.assertTrue(selector.select(5),'owner did not launch supervisor')
                    supervisor_pid = int(owner.stdout.readline())
                deadline=time.monotonic()+4
                while not marker.exists() and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists(),'isolated child did not start')
                child_pid=int(marker.read_text())
                owner.kill()
                self.assertEqual(owner.wait(timeout=5),-signal.SIGKILL)
                result=None
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    try:
                        result=json.loads((root/'result.json').read_text())
                        break
                    except (FileNotFoundError,ValueError):
                        time.sleep(.01)
                self.assertIsNotNone(result,(root/'supervisor-error').read_text())
                self.assertEqual(result['reason'],'owner_lost')
                self.assertEqual(result['returncode'],-signal.SIGKILL)
                durable = CheckJournal(journal_path).read('a'*64)
                self.assertEqual(durable['state'],'completed')
                self.assertEqual(durable['result'],result)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid,0)
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.wait(timeout=5)
                owner.stdout.close()
                owner.stderr.close()
                # The supervisor has a bounded child lifetime even on failure;
                # allow it to observe EOF before deleting its owned workspace.
                if supervisor_pid is not None:
                    deadline=time.monotonic()+7
                    while time.monotonic()<deadline:
                        try:
                            os.kill(supervisor_pid,0)
                        except ProcessLookupError:
                            break
                        time.sleep(.02)
                    else:
                        self.fail('supervisor did not retire')
