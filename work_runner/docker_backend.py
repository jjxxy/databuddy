"""Narrow Docker API client; only the independent runner holds the socket."""
import base64
import json
import os
import socket
import struct
import threading
import time

import httpx

from .protocol import MAX_ARCHIVE, MAX_LOG, unpack


class DockerBackend:
    def __init__(self, image=None, socket=None):
        self.image = image or os.environ['ATLAS_SANDBOX_IMAGE']
        if '@sha256:' not in self.image or len(self.image.rsplit('@sha256:', 1)[1]) != 64:
            raise ValueError('ATLAS_SANDBOX_IMAGE must use an approved immutable image digest')
        self.socket_path=socket or '/var/run/docker.sock'
        self.client = httpx.Client(transport=httpx.HTTPTransport(uds=self.socket_path),
                                  base_url='http://docker/v1.45', timeout=15, trust_env=False)

    def request(self, method, path, **kwargs):
        result = self.client.request(method, path, **kwargs)
        result.raise_for_status()
        return result.json() if result.content else {}

    def ready(self):
        info = self.request('GET', '/info')
        if 'runsc' not in info.get('Runtimes', {}):
            raise ValueError('Docker gVisor runtime runsc is not installed; refusing execution')
        if any(info.get(name) is not True for name in ('MemoryLimit','SwapLimit','PidsLimit','CpuCfsQuota')):
            raise ValueError('Docker host does not support required memory, swap, PID and CPU limits')
        if any(argument in ('--ignore-cgroups','--ignore-cgroups=true') for argument in info['Runtimes']['runsc'].get('Args',[])):
            raise ValueError('gVisor must enforce cgroups; ignore-cgroups is not allowed')
        image = self.request('GET', '/images/' + self.image + '/json')
        return {'runtime': 'runsc', 'image_id': image['Id'], 'network': 'none'}

    def create(self, job):
        self.ready()
        memory = 1024 * 1024 * 1024
        config = {
            'Image': self.image, 'User': '10001:10001', 'WorkingDir': '/workspace',
            'Cmd': ['python', '-I', '/opt/atlas-work/transfer.py', 'idle'],
            'Env': ['PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=/tmp', 'LANG=C.UTF-8',
                    'PYTHONDONTWRITEBYTECODE=1', 'MPLCONFIGDIR=/tmp/matplotlib'],
            'Labels': {'atlas.work.execution': job['execution_id'], 'atlas.work.workspace': job['workspace_id']},
            'HostConfig': {
                'Runtime': 'runsc', 'NetworkMode': 'none', 'ReadonlyRootfs': True,
                'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true'],
                'Memory': memory, 'MemorySwap': memory, 'NanoCpus': 1000000000, 'PidsLimit': 128,
                'Tmpfs': {'/workspace': 'rw,nosuid,nodev,size=96m,uid=10001,gid=10001,mode=0700',
                          '/tmp': 'rw,nosuid,nodev,size=96m,uid=10001,gid=10001,mode=0700'},
                'LogConfig': {'Type': 'local', 'Config': {'max-size': '1m', 'max-file': '1'}},
                'RestartPolicy': {'Name': 'no'}, 'Ulimits': [{'Name': 'nofile', 'Soft': 1024, 'Hard': 1024}],
            },
        }
        return self.request('POST', '/containers/create', params={'name': 'atlas-work-' + job['execution_id']}, json=config)['Id']

    def start(self, container):
        self.request('POST', '/containers/' + container + '/start')
        info = self.request('GET', '/containers/' + container + '/json')
        config = info['HostConfig']
        if (config['Runtime'] != 'runsc' or config['NetworkMode'] != 'none'
                or not config['ReadonlyRootfs'] or config.get('Privileged') or config.get('Binds')
                or info['Config']['User'] != '10001:10001'
                or any(mount.get('Type') != 'tmpfs' or mount.get('Destination') not in ('/workspace', '/tmp') for mount in info.get('Mounts', []))):
            self.remove(container)
            raise ValueError('Sandbox runtime inspection failed')

    def stage(self, container, archive):
        self.transfer(container,'import',input_bytes=archive,limit=1024)

    def prepare_exec(self, container, command, timeout):
        return self.request('POST', '/containers/' + container + '/exec', json={
            'User': '10001:10001', 'WorkingDir': '/workspace', 'AttachStdout': False, 'AttachStderr': False,
            'Cmd': ['python', '-I', '/opt/atlas-work/guest.py', base64.b64encode(command.encode()).decode(), str(timeout)],
        })['Id']

    def start_exec(self, execution):
        self.request('POST', '/exec/' + execution + '/start', json={'Detach': True, 'Tty': False})

    def inspect_exec(self, execution):
        return self.request('GET', '/exec/' + execution + '/json')

    def quiesce(self, container):
        self.transfer(container,'quiesce',limit=1024)

    def transfer(self,container,mode,*,input_bytes=None,limit):
        """Docker exec's duplex raw stream reaches gVisor's in-memory mounts.

        Host-side Docker archive/cp cannot read these tmpfs files. Never
        replace this with a bind mount of an application host directory.
        """
        execution=self.request('POST','/containers/'+container+'/exec',json={
            'User':'10001:10001','WorkingDir':'/workspace','AttachStdin':input_bytes is not None,
            'AttachStdout':True,'AttachStderr':True,'Tty':False,
            'Cmd':['python','-I','/opt/atlas-work/transfer.py',mode],
        })['Id']
        body=b'{"Detach":false,"Tty":false}'
        header=('POST /v1.45/exec/'+execution+'/start HTTP/1.1\r\nHost: docker\r\n'
                'Connection: Upgrade\r\nUpgrade: tcp\r\nContent-Type: application/json\r\n'
                'Content-Length: '+str(len(body))+'\r\n\r\n').encode()
        output=bytearray();errors=bytearray();pending=bytearray();writer=None
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as channel:
            channel.settimeout(60);channel.connect(self.socket_path);channel.sendall(header+body)
            while b'\r\n\r\n' not in pending:
                chunk=channel.recv(4096)
                if not chunk or len(pending)>16384:raise ValueError('Invalid Docker exec upgrade response')
                pending.extend(chunk)
            headers,remaining=bytes(pending).split(b'\r\n\r\n',1)
            if not headers.startswith(b'HTTP/1.1 101 '):raise ValueError('Docker exec duplex stream was not upgraded')
            pending=bytearray(remaining)
            if input_bytes is not None:
                def send():
                    try:channel.sendall(input_bytes);channel.shutdown(socket.SHUT_WR)
                    except OSError:pass
                writer=threading.Thread(target=send,daemon=True);writer.start()
            def read_exact(size):
                while len(pending)<size:
                    chunk=channel.recv(min(65536,size-len(pending)))
                    if not chunk:
                        if not pending:return None
                        raise ValueError('Truncated Docker exec output')
                    pending.extend(chunk)
                value=bytes(pending[:size]);del pending[:size];return value
            while frame:=read_exact(8):
                stream,length=frame[0],struct.unpack('>I',frame[4:])[0]
                if stream not in (1,2) or length>max(limit,64000):raise ValueError('Invalid Docker exec output frame')
                data=read_exact(length) if length else b''
                if data is None:raise ValueError('Truncated Docker exec frame')
                target=output if stream==1 else errors;target.extend(data)
                if len(output)>limit or len(errors)>64000:raise ValueError('Guest transfer exceeds output budget')
        if writer:writer.join(timeout=1)
        for _ in range(10):
            receipt=self.inspect_exec(execution)
            if not receipt['Running']:break
            time.sleep(.05)
        if receipt['Running'] or receipt.get('ExitCode')!=0:raise ValueError('Guest file transfer failed')
        return bytes(output)

    def files(self, container):
        return unpack(self.transfer(container,'export',limit=MAX_ARCHIVE))

    def logs(self, container):
        value=json.loads(self.transfer(container,'logs',limit=MAX_LOG*12+1024))
        return {name:str(value.get(name,''))[:MAX_LOG] for name in ('stdout','stderr')}

    def remove(self, container):
        response = self.client.delete('/containers/' + container, params={'force': 'true', 'v': 'true'})
        if response.status_code != 404:
            response.raise_for_status()
