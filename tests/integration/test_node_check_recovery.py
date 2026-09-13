from pathlib import Path
import tempfile
import unittest

from multi_shadow_clone.execution.domain.admission import Rejected
from multi_shadow_clone.execution.domain.checks import NodeCheck
from multi_shadow_clone.execution.infrastructure.container_journal import ContainerJournal
from multi_shadow_clone.execution.infrastructure.node_checks import NodeChecks, fingerprint


class NodeCheckRecoveryTest(unittest.TestCase):
    def test_receipt_requires_matching_binding_and_confirmed_supervision(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=ContainerJournal(Path(directory)/'containers.sqlite3')
            definition=NodeCheck('sample','check.js',(('check.js','a'*64),),2,1024,100)
            checker=NodeChecks(definitions=[definition],files=None,profile={'fixture':'local'},
                service_factory=None,journal=journal)
            contract=checker.contract_hash('sample')
            config={'fixture':'container-runtime'}
            context={'payload_hash':'b'*64,'receipt':{'check_id':'sample','definition_hash':contract,
                'runtime_hash':fingerprint(config)}}
            journal.claim('c'*64,config,context=context)
            journal.advance('c'*64,config,'reserve_create')
            journal.advance('c'*64,config,'created',container_id='d'*64)
            journal.claim_supervisor('c'*64,config,123)
            journal.reserve_attached('c'*64,config,timeout=2,max_output_bytes=100)
            journal.record_output('c'*64,config,{'reason':'completed','returncode':0,'output_bytes':0})
            journal.advance('c'*64,config,'exited',result={'exit_code':0,'oom_killed':False})
            self.assertEqual(checker.completion('c'*64,'b'*64,contract)['state'],'unknown')
            journal.complete_supervisor('c'*64,config,123,'deadline')
            self.assertFalse(checker.completion('c'*64,'b'*64,contract)['receipt']['passed'])
            with self.assertRaises(Rejected): checker.completion('c'*64,'e'*64,contract)
            with self.assertRaises(Rejected): checker.completion('c'*64,'b'*64,'e'*64)
