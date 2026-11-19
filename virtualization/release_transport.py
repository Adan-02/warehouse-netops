"""Pinned, bounded SSH transport for the final service network.

Only the owned Guangzhou management connection is opened from Windows. The
service key remains on Guangzhou, which binds the dedicated tunnel source.
"""
from __future__ import annotations

from contextlib import nullcontext
import io
from pathlib import Path
import shlex
import threading
import time

try:
    from .cloud_connection import connect_guangzhou
except ImportError:
    from cloud_connection import connect_guangzhou

PRIVATE = '/var/lib/warehouse-netops-guangzhou/private'


def service_command(command):
    if not isinstance(command, str) or not command or '\x00' in command:
        raise ValueError('A nonempty service command is required')
    args = ['sudo','-n','ssh','-F','/dev/null','-b','10.89.0.1',
        '-i',PRIVATE+'/maintenance_ed25519','-o','BatchMode=yes',
        '-o','ConnectTimeout=8','-o','StrictHostKeyChecking=yes',
        '-o','UserKnownHostsFile='+PRIVATE+'/service_known_hosts',
        '-o','GlobalKnownHostsFile=/dev/null','-o','IdentityAgent=none',
        '-o','IdentitiesOnly=yes','-o','PreferredAuthentications=publickey',
        '-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no',
        '-o','HostKeyAlgorithms=ssh-ed25519','warehouse@192.168.30.10',command]
    return shlex.join(args)


def exchange(client, command, *, input_data=None, input_file=None, output_file=None,
             timeout=30, tail_limit=16384):
    """Stream input and both output channels concurrently, retaining bounded tails."""
    if input_data is not None and input_file is not None:
        raise ValueError('Select bytes or a file for input')
    if timeout <= 0 or tail_limit < 1:
        raise ValueError('Timeout and output bound must be positive')
    if isinstance(input_data,str):
        input_data=input_data.encode('utf-8')
    transport=client.get_transport()
    if transport is None or not transport.is_active():
        raise ConnectionError('Pinned SSH transport is not active')
    channel=transport.open_session(timeout=min(timeout,15))
    channel.settimeout(min(timeout,15))
    errors=[]
    sent=threading.Event()
    def sender():
        try:
            source = open(input_file,'rb') if input_file is not None else io.BytesIO(input_data or b'')
            with source:
                while True:
                    data=source.read(65536)
                    if not data: break
                    channel.sendall(data)
            channel.shutdown_write()
        except BaseException as error:
            errors.append(error)
        finally:
            sent.set()
    output=b''; stderr=b''
    destination = open(output_file,'xb') if output_file is not None else None
    try:
        channel.exec_command(command)
        thread=threading.Thread(target=sender,daemon=True)
        thread.start()
        deadline=time.monotonic()+timeout
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('Project SSH operation timed out; query release status before retrying')
            if errors:
                raise ConnectionError('Project SSH upload failed') from errors[0]
            progress=False
            while channel.recv_ready():
                chunk=channel.recv(65536)
                if destination is not None:
                    destination.write(chunk)
                else:
                    output=(output+chunk)[-tail_limit:]
                progress=True
            while channel.recv_stderr_ready():
                chunk=channel.recv_stderr(65536)
                stderr=(stderr+chunk)[-tail_limit:]
                progress=True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if not progress: time.sleep(.01)
        code=channel.recv_exit_status()
        if not sent.wait(timeout=2):
            raise ConnectionError('Remote operation ended before upload completed')
        if errors:
            raise ConnectionError('Project SSH input was not fully delivered') from errors[0]
        if destination is not None:
            destination.flush()
        text=output.decode('utf-8',errors='replace')
        if stderr:
            text=(text+'\n'+stderr.decode('utf-8',errors='replace'))[-tail_limit:]
        return code,text
    finally:
        channel.close()
        if destination is not None: destination.close()


class ServiceRelay:
    def __init__(self, client=None):
        self.client=client or connect_guangzhou()

    def cloud_run(self, command, **kwargs):
        return exchange(self.client, command, **kwargs)

    def run(self, command, **kwargs):
        return self.cloud_run(service_command(command), **kwargs)

    def close(self):
        if self.client is not None:
            self.client.close(); self.client=None

    def __enter__(self): return self

    def __exit__(self, *args): self.close()
