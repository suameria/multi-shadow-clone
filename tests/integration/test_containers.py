from copy import deepcopy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest

from kagebunshin.execution.application.containers import Containers
from kagebunshin.execution.domain.admission import Rejected
from kagebunshin.execution.infrastructure.container_journal import ContainerJournal
from kagebunshin.execution.infrastructure.docker_sandbox import NodeContainer


class Runtime:
    def execution_scope(self,timeout,stopped): return nullcontext()
    def __init__(self):
        self.fixture=json.loads((Path(__file__).resolve().parents[1]/'fixtures/docker-created.json').read_text())
        self.spec=NodeContainer(self.fixture['Image'],'a'*32,tuple(self.fixture['Config']['Cmd']))
        self.observed=None
        self.creates=self.removes=0
    def contract(self): return {'fixture':'owned-container'}
    def create(self):
        self.creates+=1
        self.observed=deepcopy(self.fixture)
        raise OSError('lost create response')
    def inspect(self): return deepcopy(self.observed)
    def verify_created(self,value): self.spec.verify_created(value['Id'],value)
    def verify_owned(self,value): self.spec.verify_owned(value['Id'],value)
    def remove(self,container_id):
        self.removes+=1
        self.observed=None
        raise OSError('lost removal response')


class ContainersTest(unittest.TestCase):
    def test_attached_output_is_durable_and_cli_timeout_stops_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'containers.sqlite3'
            runtime=Runtime()
            service=Containers(ContainerJournal(path),runtime)
            with self.assertRaises(OSError): service.prepare('a'*64)
            service.recover_created('a'*64)
            calls=[]
            def attached(container_id,**limits):
                calls.append('attach')
                runtime.observed['State'].update(Status='running',Running=True,Pid=123)
                return {'reason':'timeout','output_bytes':3,'stdout':'abc','stderr':'','returncode':-9}
            def kill(container_id):
                calls.append('kill')
                runtime.observed['State'].update(Status='exited',Running=False,Pid=0,ExitCode=137,OOMKilled=False)
            runtime.start_attached=attached
            runtime.kill=kill
            result=service.execute_attached('a'*64,timeout=1,max_output_bytes=10,stopped=lambda:False)
            self.assertEqual(result['output']['reason'],'timeout')
            self.assertEqual(result['result']['exit_code'],137)
            self.assertEqual(ContainerJournal(path).read('a'*64),result)
            with self.assertRaises(Rejected):
                service.execute_attached('a'*64,timeout=1,max_output_bytes=10,stopped=lambda:False)
            with self.assertRaises(Rejected):
                service.journal.record_output('a'*64,runtime.contract(),{**result['output'],'stdout':'changed'})
            self.assertEqual(calls,['attach','kill'])

    def test_lost_start_and_stop_are_reconciled_without_restarting(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'containers.sqlite3'
            runtime=Runtime()
            service=Containers(ContainerJournal(path),runtime)
            with self.assertRaises(OSError): service.prepare('a'*64)
            service.recover_created('a'*64)
            calls=[]
            def lost_start(container_id):
                calls.append('start')
                runtime.observed['State'].update(Status='running',Running=True,Pid=123)
                raise OSError('lost start response')
            def lost_kill(container_id):
                calls.append('kill')
                runtime.observed['State'].update(Status='exited',Running=False,Pid=0,ExitCode=137,OOMKilled=False)
                raise OSError('lost stop response')
            runtime.start=lost_start
            runtime.kill=lost_kill
            with self.assertRaises(OSError): service.start('a'*64)
            restarted=Containers(ContainerJournal(path),runtime)
            self.assertEqual(restarted.reconcile_execution('a'*64)['phase'],'running')
            with self.assertRaises(Rejected): restarted.start('a'*64)
            with self.assertRaises(Rejected): restarted.retire('a'*64)
            with self.assertRaises(OSError): restarted.stop('a'*64)
            result=restarted.reconcile_execution('a'*64)
            self.assertEqual(result['result'],{'exit_code':137,'oom_killed':False})
            self.assertEqual(calls,['start','kill'])

    def test_response_loss_recovery_and_foreign_replacement_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'containers.sqlite3'
            runtime=Runtime()
            service=Containers(ContainerJournal(path),runtime)
            with self.assertRaises(OSError): service.prepare('a'*64)
            service=Containers(ContainerJournal(path),runtime)
            service.recover_created('a'*64)
            self.assertEqual(runtime.creates,1)
            runtime.observed['Id']='c'*64
            with self.assertRaises(Rejected): service.retire('a'*64)
            self.assertEqual(runtime.removes,0)
            runtime.observed=deepcopy(runtime.fixture)
            with self.assertRaises(OSError): service.retire('a'*64)
            retired=Containers(ContainerJournal(path),runtime).retire('a'*64)
            self.assertEqual(retired['phase'],'retired')
            self.assertEqual((runtime.creates,runtime.removes),(1,1))
            self.assertNotIn('result',retired)
