from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from multi_shadow_clone.execution.infrastructure.check_process import run_check_process


class CheckProcessTest(unittest.TestCase):
    def run_script(self, script, root, *, timeout=2, maximum=1024, stopped=lambda:False):
        return run_check_process(argv=[sys.executable, '-I', '-S', '-c', script],
                                 cwd=root, env={}, timeout=timeout,
                                 max_output_bytes=maximum, stopped=stopped)

    def test_normal_nonzero_exit_and_both_output_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_script("import sys;print('out');print('err',file=sys.stderr);sys.exit(3)", directory)
            self.assertEqual(result['reason'], 'completed')
            self.assertEqual(result['returncode'], 3)
            self.assertEqual(result['stdout'], 'out\n')
            self.assertEqual(result['stderr'], 'err\n')
            self.assertEqual(result['output_bytes'], 8)

    def test_timeout_and_output_overflow_kill_and_reap_the_child(self):
        with tempfile.TemporaryDirectory() as directory:
            timed = self.run_script('while True: pass', directory, timeout=.15)
            self.assertEqual(timed['reason'], 'timeout')
            self.assertLess(timed['returncode'], 0)
            flooded = self.run_script("import os\nwhile True: os.write(1,b'x'*8192)", directory, maximum=100)
            self.assertEqual(flooded['reason'], 'output_limit')
            self.assertEqual(flooded['output_bytes'], 100)
            self.assertEqual(flooded['stdout'], 'x'*100)
            self.assertLess(flooded['returncode'], 0)

    def test_stop_after_start_and_observer_failure_reap_the_child(self):
        with tempfile.TemporaryDirectory() as directory:
            started = Path(directory)/'started'
            script = f"from pathlib import Path\nPath({str(started)!r}).write_text('yes')\nwhile True: pass"
            stopped = self.run_script(script, directory, stopped=started.exists)
            self.assertEqual(stopped['reason'], 'stopped')
            self.assertLess(stopped['returncode'], 0)
            started.unlink()
            def failing_observer():
                if started.exists():
                    raise RuntimeError('observer unavailable')
                return False
            before = time.monotonic()
            children = []
            real_popen = subprocess.Popen
            def launch(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child
            with patch('multi_shadow_clone.execution.infrastructure.check_process.subprocess.Popen', side_effect=launch):
                with self.assertRaisesRegex(RuntimeError, 'observer unavailable'):
                    self.run_script(script, directory, stopped=failing_observer)
            self.assertEqual(len(children), 1)
            self.assertLess(children[0].returncode, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(children[0].pid, os.WNOHANG)
            self.assertLess(time.monotonic()-before, 2)
