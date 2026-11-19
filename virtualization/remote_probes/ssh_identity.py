"""Reuse the prepared maintenance key and strict host pin without creating files."""
import re
import stat
import subprocess
from pathlib import Path


def ssh_identity(parameters):
    result = {'source_ip': parameters['source_ip'], 'target_ip': parameters['service_ip'],
        'exit_code': None, 'identity_matches': False, 'private_paths_guarded': False,
        'authentication': 'publickey', 'host_key_policy': 'strict project pin'}
    try:
        root = Path('/var/lib/warehouse-netops-guangzhou')
        private = root / 'private'

        def guard(path, directory=False):
            info = path.lstat()
            kind = stat.S_ISDIR if directory else stat.S_ISREG
            if not kind(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
                raise RuntimeError('Unsafe existing project private path')

        for path in (root, private):
            guard(path, True)
        for path in (root / '.owner', private / 'maintenance_ed25519', private / 'service_known_hosts'):
            guard(path)
        if not re.fullmatch('warehouse-netops-guangzhou-v1:[0-9a-f]{32}', (root / '.owner').read_text().strip()):
            raise RuntimeError('Existing project ownership marker invalid')
        result['private_paths_guarded'] = True
        args = ['ssh', '-F', '/dev/null', '-i', str(private / 'maintenance_ed25519'), '-b', parameters['source_ip'],
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(private / 'service_known_hosts'), '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes', '-o', 'PreferredAuthentications=publickey',
            '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no', '-o', 'HostKeyAlgorithms=ssh-ed25519',
            'warehouse@' + parameters['service_ip'], 'id -u; id -un; hostname']
        completed = subprocess.run(args, capture_output=True, text=True, timeout=12)
        result['exit_code'] = completed.returncode
        result['identity_matches'] = completed.returncode == 0 and completed.stdout.strip().splitlines() == ['1000', 'warehouse', 'warehouse-service']
        if result['identity_matches']:
            result['identity'] = {'uid': 1000, 'user': 'warehouse', 'hostname': 'warehouse-service'}
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    return result
