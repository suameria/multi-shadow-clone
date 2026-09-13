from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest

from kagebunshin.execution.domain.admission import Rejected
from kagebunshin.execution.infrastructure.check_workspace import check_workspace
from kagebunshin.execution.infrastructure.docker_sandbox import NodeContainer, SnapshotMount


class SnapshotMountTest(unittest.TestCase):
    def test_owned_read_only_mount_rejects_writable_or_replaced_snapshot(self):
        data='console.log(42)'
        inputs=[{'path':'check.js','content':data,'bytes':len(data),'sha256':sha256(data.encode()).hexdigest()}]
        with check_workspace(inputs,max_bytes=100) as snapshot:
            info=snapshot.root.stat()
            if info.st_uid==0:
                self.skipTest('this profile requires a non-root host owner')
            mount=SnapshotMount(str(snapshot.root),info.st_dev,info.st_ino,snapshot.ownership['marker_hash'],info.st_uid,info.st_gid)
            observed=json.loads((Path(__file__).resolve().parents[1]/'fixtures/docker-created.json').read_text())
            request=NodeContainer(observed['Image'],'a'*32,('/inputs/check.js',),mount)
            observed['Config']['User']=request.user()
            observed['Config']['Cmd']=list(request.arguments)
            observed['HostConfig']['Mounts']=[{'Type':'bind','Source':str(snapshot.root),'Target':'/inputs','ReadOnly':True,'BindOptions':{'NonRecursive':True}}]
            observed['Mounts']=[{'Type':'bind','Source':str(snapshot.root),'Destination':'/inputs','Mode':'','RW':False,'Propagation':'rprivate'}]
            request.verify_created('b'*64,observed)
            changed=deepcopy(observed)
            changed['Mounts'][0]['RW']=True
            with self.assertRaises(Rejected): request.verify_created('b'*64,changed)
            changed=deepcopy(observed)
            changed['HostConfig']['Mounts'][0]['ReadOnly']=1
            with self.assertRaises(Rejected): request.verify_created('b'*64,changed)
            marker=snapshot.root/'.kagebunshin-owner'
            marker.chmod(0o600)
            marker.write_text('replaced')
            with self.assertRaises(Rejected): request.create_arguments()
