import unittest
from copy import deepcopy
from kagebunshin.execution.domain.retirement import validate_retirement
from kagebunshin.execution.domain.admission import Rejected

class RetirementTest(unittest.TestCase):
    def setUp(self):
        self.attempt=dict(id='a',node_id='n',stage='audit',state='terminal',terminal_status='completed',binding_hash='h',thread_id='t',turn_id='u')
        self.run=dict(id='r',stopped=True,stop_epoch=1,nodes={'n':{'active':None}},attempts=[self.attempt],execution_policy={'scopes':[{'node_id':'n','stage':'audit'}]})
        self.grant={'grant':{'binding':dict(run_id='r',attempt_id='a',node_id='n',contract_hash='h',thread_id='t',turn_id='u')},'stopped':True,'calls':{'c':{'operation':'run_check','state':'completed'}}}
    def test_check_resource_proof_and_fences_are_required(self):
        with self.assertRaises(Rejected):validate_retirement(self.run,{'a':self.grant},{})
        proof={('a','c'):{'retired':True}}
        validate_retirement(self.run,{'a':self.grant},proof)
        self.grant['calls']['c']['state']='unknown'
        with self.assertRaises(Rejected):validate_retirement(self.run,{'a':self.grant},proof)
    def test_missing_dispatched_history_and_active_job_reject(self):
        with self.assertRaises(Rejected):validate_retirement(self.run,{}, {})
        self.attempt.update(terminal_status='not_sent',turn_id=None)
        validate_retirement(self.run,{}, {})
        self.run['nodes']['n']['active']='a'
        with self.assertRaises(Rejected):validate_retirement(self.run,{}, {})
        self.run['nodes']['n']['active']=None;self.run['stopped']=False
        with self.assertRaises(Rejected):validate_retirement(self.run,{}, {})
