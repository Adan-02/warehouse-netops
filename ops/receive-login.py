#!/usr/bin/python3
"""Fixed SSH login wrapper: validate this path, then drop to the observer UID.

The service image permits only its existing warehouse login through AllowUsers.
This independent restricted key cannot run that user's shell or use its sudo
rights: its only command drops immediately to the unprivileged receiver.
"""
import os
import re


def receiver_arguments(environment=None):
    environment=os.environ if environment is None else environment
    connection=environment.get('SSH_CONNECTION','').split()
    if (len(connection)!=4 or connection[0]!='10.89.0.1' or connection[2]!='192.168.30.10'
            or not all(re.fullmatch(r'[0-9]{1,5}',connection[i]) and 1<=int(connection[i])<=65535 for i in (1,3))):
        raise ValueError('Only the dedicated reporting source and destination are accepted')
    if environment.get('SSH_ORIGINAL_COMMAND')!='warehouse-report-v1':
        raise ValueError('Only the fixed reporting protocol is accepted')
    return ['/usr/bin/sudo','-n','-u','warehouse-observer','--','/usr/bin/env',
        'PYTHONPATH=/opt/warehouse-netops-observer',
        'SSH_CONNECTION='+' '.join(connection),'SSH_ORIGINAL_COMMAND=warehouse-report-v1',
        '/usr/bin/python3','-m','ops.receiver','--directory',
        '/var/lib/warehouse-netops-observer/observations']


def main():
    try:
        arguments=receiver_arguments()
        os.execv(arguments[0],arguments)
    except Exception as error:
        # Include only the error type, never client input or private state.
        print('{"accepted":false,"error_type":"'+type(error).__name__+'"}')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
