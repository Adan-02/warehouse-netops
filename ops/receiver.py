"""Fixed OpenSSH forced-command receiver; it never executes input."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from .common import MAX_SNAPSHOT_BYTES, atomic_json, validate_snapshot


def receive(stream, directory, environment=None, now=None):
    environment = os.environ if environment is None else environment
    connection = environment.get('SSH_CONNECTION', '').split()
    if len(connection) != 4 or connection[0] != '10.89.0.1' or connection[2] != '192.168.30.10':
        raise ValueError('Reporter source or destination is not the dedicated VPN path')
    if environment.get('SSH_ORIGINAL_COMMAND') != 'warehouse-report-v1':
        raise ValueError('Only the fixed reporting protocol is accepted')
    payload = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if not payload or len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Reporter payload exceeds limit or is empty')
    value = json.loads(payload)
    value = validate_snapshot(value, 'guangzhou', now=time.time() if now is None else now)
    atomic_json(Path(directory) / 'remote.json', value)
    return {'accepted': True, 'received_at': value['received_at']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        result = receive(sys.stdin.buffer, args.directory)
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'accepted': False, 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
