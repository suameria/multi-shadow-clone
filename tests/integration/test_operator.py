"""Actual loopback JSON transport and host worker lifetime; no browser rendering."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

from multi_shadow_clone.orchestration.infrastructure.operator_jobs import OperatorJobs
from multi_shadow_clone.orchestration.infrastructure.operator_server import OperatorServer


class OperatorIntegrationTest(unittest.TestCase):
    def test_json_transport_rejects_cross_origin_and_ambiguous_actions(self):
        class Operator:
            def __init__(self): self.calls = []
            def snapshot(self): return {"roles": [], "jobs": []}
            def settings_snapshot(self): return {"available": True, "revision": 1}
            def save_settings(self, value, expected_revision):
                from multi_shadow_clone.orchestration.ports import Conflict
                if expected_revision != 1: raise Conflict("stale settings")
                self.calls.append(value)
                return {"revision": 2}

            def submit(self, **args):
                self.calls.append(args)
                return {"state": "saved"}
            def act(self, op, run):
                self.calls.append((op, run))
                return {"state": "stopped"}
        operator = Operator()
        server = OperatorServer(operator, "offline")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        def call(method, path, body=None, **headers):
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                client.request(method, path, body, headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally: client.close()
        try:
            self.assertEqual(call("GET", "/api/state")[1]["mode"], "offline")
            self.assertEqual(call("GET", "/api/state", Host="elsewhere.invalid")[0], 403)
            self.assertEqual(call("GET", "/../runtime/secret")[0], 404)
            body = json.dumps({"operation": "stop", "run_id": "synthetic-id"})
            headers = {"Content-Type": "application/json", "Origin": server.origin,
                       "X-Multi-Shadow-Clone-CSRF": server.csrf}
            self.assertEqual(call("POST", "/api/action", body)[0], 403)
            wrong = {**headers, "Origin": "https://elsewhere.invalid"}
            self.assertEqual(call("POST", "/api/action", body, **wrong)[0], 403)
            ambiguous = '{"operation":"run","operation":"stop","run_id":"synthetic-id"}'
            self.assertEqual(call("POST", "/api/action", ambiguous, **headers)[0], 409)
            self.assertEqual(operator.calls, [])
            self.assertEqual(call("POST", "/api/action", body, **headers)[0], 200)
            self.assertEqual(operator.calls, [("stop", "synthetic-id")])
            request = json.dumps({"objective": "test", "source": "fixture", "max_turns": 8})
            self.assertEqual(call("POST", "/api/requests", request, **wrong)[0], 403)
            self.assertEqual(call("POST", "/api/requests", request, **headers)[0], 200)
            self.assertEqual(operator.calls[-1], {"objective": "test", "source": "fixture", "max_turns": 8})
            self.assertEqual(call("GET", "/api/settings")[1]["revision"], 1)
            settings = json.dumps({"value": {}, "expected_revision": 1})
            self.assertEqual(call("POST", "/api/settings", settings, **wrong)[0], 403)
            self.assertEqual(call("POST", "/api/settings", settings, **headers)[1]["revision"], 2)
            stale = json.dumps({"value": {}, "expected_revision": 0})
            self.assertEqual(call("POST", "/api/settings", stale, **headers)[0], 409)

        finally:
            server.shutdown(); server.server_close(); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_shutdown_cancels_owned_driver_and_retains_failure_status(self):
        jobs = OperatorJobs()
        started, cancel = threading.Event(), threading.Event()
        def work():
            started.set()
            if not cancel.wait(2): raise RuntimeError("shutdown did not stop owned job")
        jobs.start("fixture", work, cancel.set)
        self.assertTrue(started.wait(1))
        with self.assertRaises(ValueError): jobs.start("other", lambda: None, lambda: None)
        jobs.close()
        self.assertTrue(cancel.is_set())
        self.assertEqual(jobs.status()["state"], "returned")
        with self.assertRaises(ValueError): jobs.start("after", lambda: None, lambda: None)
        failed = OperatorJobs()
        def fail(): raise RuntimeError("synthetic private detail")
        failed.start("failure", fail, lambda: None)
        try:
            failed.current.result(timeout=1)
        except RuntimeError: pass
        self.assertEqual(failed.status(), {"id": "failure", "state": "failed", "error": "RuntimeError"})
        failed.close()

    def test_sigterm_closes_the_real_cli_server_and_owned_job_lifetime(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            code = '''
from contextlib import contextmanager
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from multi_shadow_clone.orchestration.presentation.operator_cli import main
from multi_shadow_clone.orchestration.infrastructure.operator_server import OperatorServer
marker = Path(sys.argv[2])
@contextmanager
def scope(mode, data_dir=None, workspace_config=None):
    assert data_dir == marker.parent / "state"
    assert workspace_config == marker.parent / "workspace.json"
    try: yield None
    finally: marker.with_suffix('.engine').write_text('closed')
class Jobs:
    def close(self): marker.write_text('closed')
class Operator:
    def __init__(self, *args, **kwargs): pass
    def snapshot(self): return {'jobs': [], 'roles': []}
main(scope, lambda engine: None, Operator, Jobs, OperatorServer, ['--mode', 'offline', '--data-dir', str(marker.parent / 'state'), '--workspace-config', str(marker.parent / 'workspace.json')])
'''
            marker = Path(folder) / "closed"
            process = subprocess.Popen([sys.executable, "-I", "-c", code, str(root / "src"), str(marker)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = process.stdout.readline()
                self.assertTrue(line.startswith("http://127.0.0.1:"), line)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(marker.read_text(), "closed")
                self.assertEqual(marker.with_suffix(".engine").read_text(), "closed")
            finally:
                if process.poll() is None: process.kill(); process.communicate(timeout=5)
