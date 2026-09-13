"""Build and verify a host-selected, cached Node container before starting it.

No Docker calls occur here. This is not yet a model-facing check adapter.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import stat

from ..domain.admission import Rejected, content_hash


@dataclass(frozen=True)
class SnapshotMount:
    directory: str
    device: int
    inode: int
    marker_hash: str
    uid: int
    gid: int

    def validate(self):
        content_hash(self.marker_hash)
        path=Path(self.directory)
        if (not path.is_absolute() or str(path.resolve(strict=True))!=self.directory
            or not path.name.startswith('multi-shadow-clone-check-') or ',' in self.directory
            or type(self.uid) is not int or not 0<self.uid<2147483647
            or type(self.gid) is not int or not 0<=self.gid<2147483647):
            raise Rejected('invalid owned snapshot mount')
        info=path.lstat()
        marker=path/'.multi-shadow-clone-owner'
        marker_info=marker.lstat()
        if (not stat.S_ISDIR(info.st_mode) or (info.st_dev,info.st_ino)!=(self.device,self.inode)
            or (info.st_uid,info.st_gid)!=(self.uid,self.gid)
            or not stat.S_ISREG(marker_info.st_mode) or marker_info.st_nlink!=1
            or marker_info.st_size>4096 or sha256(marker.read_bytes()).hexdigest()!=self.marker_hash):
            raise Rejected('snapshot ownership changed')


@dataclass(frozen=True)
class NodeContainer:
    image_id: str
    owner: str
    arguments: tuple[str, ...]
    snapshot: SnapshotMount | None = None

    def validate(self):
        if self.snapshot is not None:
            if not isinstance(self.snapshot,SnapshotMount):
                raise Rejected('invalid snapshot contract')
            self.snapshot.validate()
        if type(self.image_id) is not str or not self.image_id.startswith('sha256:'):
            raise Rejected('container image must be immutable')
        content_hash(self.image_id[7:])
        if type(self.owner) is not str or not re.fullmatch('[0-9a-f]{32}',self.owner):
            raise Rejected('invalid host container ownership')
        if (type(self.arguments) is not tuple or not self.arguments
            or not all(type(arg) is str and '\x00' not in arg for arg in self.arguments)
            or sum(len(arg.encode()) for arg in self.arguments)>65536):
            raise Rejected('invalid host container command')

    def create_arguments(self):
        self.validate()
        arguments=['create','--pull','never','--name','multi-shadow-clone-'+self.owner,
                '--label','io.multi-shadow-clone.owner='+self.owner,'--label','io.multi-shadow-clone.kind=bounded-check-v1',
                '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges=true',
                '--ipc','private','--cgroupns','private','--memory','64m','--memory-swap','64m',
                '--cpus','0.5','--pids-limit','16','--ulimit','cpu=3:3','--shm-size','1m',
                '--tmpfs','/work:rw,noexec,nosuid,nodev,size=1048576,mode=1777',
                '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=1048576,mode=1777',
                '--log-driver','none',
                '--user',self.user(),'--workdir','/work','--entrypoint','/usr/local/bin/node']
        if self.snapshot is not None:
            arguments+=['--mount','type=bind,src='+self.snapshot.directory+',dst=/inputs,readonly,bind-recursive=disabled']
        return [*arguments,self.image_id,*self.arguments]

    def user(self):
        return '65534:65534' if self.snapshot is None else f'{self.snapshot.uid}:{self.snapshot.gid}'

    def verify_created(self,container_id,inspection):
        receipt=self.verify_owned(container_id,inspection)
        state=inspection['State']
        if (state.get('Status')!='created' or state.get('Running') is not False
            or state.get('Paused') is not False or state.get('Restarting') is not False
            or type(state.get('Pid')) is not int or state['Pid']!=0 or state.get('Error')!=''):
            raise Rejected('container is not awaiting its first start')
        return receipt

    def verify_owned(self,container_id,inspection):
        """Check identity and restrictions, independently of execution phase."""
        self.validate()
        content_hash(container_id)
        try:
            config,host,state=inspection['Config'],inspection['HostConfig'],inspection['State']
            exact={'ReadonlyRootfs':True,'Privileged':False,'AutoRemove':False,'NetworkMode':'none',
                   'Memory':67108864,'MemorySwap':67108864,'NanoCpus':500000000,'PidsLimit':16,
                   'ShmSize':1048576,'IpcMode':'private','CgroupnsMode':'private','PidMode':'','UTSMode':'',
                   'CapDrop':['ALL'],'CapAdd':None,'SecurityOpt':['no-new-privileges=true'],
                   'PublishAllPorts':False,'RestartPolicy':{'Name':'no','MaximumRetryCount':0},
                   'LogConfig':{'Type':'none','Config':{}},
                   'Tmpfs':{'/work':'rw,noexec,nosuid,nodev,size=1048576,mode=1777',
                            '/tmp':'rw,noexec,nosuid,nodev,size=1048576,mode=1777'},
                   'Ulimits':[{'Name':'cpu','Hard':3,'Soft':3}]}
            if any(key not in host or type(host[key]) is not type(value) or host[key]!=value for key,value in exact.items()):
                raise Rejected('container restrictions differ from requested policy')
            if any(host.get(key) not in (None,[],{}) for key in
                   ('Binds','VolumesFrom','Devices','DeviceRequests','Links','ExtraHosts','PortBindings')):
                raise Rejected('container has an undeclared host attachment')
            expected_mounts=[]
            actual_mounts=[]
            if self.snapshot is not None:
                expected_mounts=[{'Type':'bind','Source':self.snapshot.directory,'Target':'/inputs',
                    'ReadOnly':True,'BindOptions':{'NonRecursive':True}}]
                actual_mounts=[{'Type':'bind','Source':self.snapshot.directory,'Destination':'/inputs',
                    'Mode':'','RW':False,'Propagation':'rprivate'}]
            if (json.dumps(host.get('Mounts') or [],sort_keys=True)!=json.dumps(expected_mounts,sort_keys=True)
                or json.dumps(inspection['Mounts'],sort_keys=True)!=json.dumps(actual_mounts,sort_keys=True)):
                raise Rejected('container input mount differs from owned snapshot')
            if (inspection['Id']!=container_id or inspection['Image']!=self.image_id
                or inspection['Name']!='/multi-shadow-clone-'+self.owner
                or config['Image']!=self.image_id or config['User']!=self.user()
                or config['Entrypoint']!=['/usr/local/bin/node'] or config['Cmd']!=list(self.arguments)
                or config['WorkingDir']!='/work' or config.get('Volumes') not in (None,{})
                or config['Labels'].get('io.multi-shadow-clone.owner')!=self.owner
                or config['Labels'].get('io.multi-shadow-clone.kind')!='bounded-check-v1'):
                raise Rejected('container identity or command differs')
            allowed_env={'PATH','NODE_VERSION','YARN_VERSION','COREPACK_DEFAULT_TO_LATEST',
                         'COREPACK_ENABLE_DOWNLOAD_PROMPT','COREPACK_HOME','PNPM_HOME'}
            if any(type(value) is not str or value.split('=',1)[0] not in allowed_env for value in config['Env']):
                raise Rejected('container has an undeclared environment variable')
        except (KeyError,TypeError,AttributeError) as exc:
            raise Rejected('container inspection is incomplete') from exc
        return {'container_id':container_id,'image_id':self.image_id,'owner':self.owner,'verified':True}
