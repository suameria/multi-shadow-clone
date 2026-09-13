from copy import deepcopy
import json
from pathlib import Path
import unittest

from kagebunshin.execution.domain.admission import Rejected
from kagebunshin.execution.infrastructure.docker_sandbox import NodeContainer


class DockerSandboxContractTest(unittest.TestCase):
    def test_observed_created_contract_rejects_weakened_or_unknown_settings(self):
        observed=json.loads((Path(__file__).resolve().parents[1]/'fixtures/docker-created.json').read_text())
        request=NodeContainer(observed['Image'],'a'*32,tuple(observed['Config']['Cmd']))
        self.assertTrue(request.verify_created('b'*64,observed)['verified'])
        cases=[('HostConfig','Memory',0),('HostConfig','MemorySwap',-1),
               ('HostConfig','NanoCpus',0),('HostConfig','PidsLimit',-1),
               ('HostConfig','ReadonlyRootfs',False),('HostConfig','ReadonlyRootfs',1),
               ('HostConfig','Privileged',True),('HostConfig','NetworkMode','host'),
               ('HostConfig','Binds',['/outside:/inside']),('HostConfig','CapAdd',['SYS_ADMIN']),
               ('HostConfig','SecurityOpt',[]),('HostConfig','LogConfig',{'Type':'json-file','Config':{}}),
               ('HostConfig','Tmpfs',{}),('Config','User','0'),
               ('Config','Env',['SECRET=fixture-only']),('Config','Cmd',['-e','changed']),
               ('State','Running',True),('State','Status','exited')]
        for section,key,value in cases:
            with self.subTest(section=section,key=key,value=value):
                changed=deepcopy(observed)
                changed[section][key]=value
                with self.assertRaises(Rejected): request.verify_created('b'*64,changed)
        for key in ('CapAdd','Memory','SecurityOpt'):
            missing=deepcopy(observed)
            del missing['HostConfig'][key]
            with self.assertRaises(Rejected): request.verify_created('b'*64,missing)
        with self.assertRaises(Rejected): request.verify_created('c'*64,observed)
        with self.assertRaises(Rejected): NodeContainer('node:latest','a'*32,('-v',)).create_arguments()
