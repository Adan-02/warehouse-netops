"""Move prepared, snapshotted lab guests to single-NIC isolated segments."""
import concurrent.futures
import json
import pathlib
import subprocess
import time
from provision_guests import connect
from runtime import PROJECT, VMS, VMRUN, isolate_guest, vmrun

ROLES = ('service', 'iot', 'office')


def prepare(role):
    client = connect(role)
    try:
        if role == 'service':
            command = ('cd /opt/warehouse-netops/server && '
                       'sudo docker compose --project-name warehouse-netops stop && '
                       'python3 setup.py --bind-ip 192.168.30.10 --demo && '
                       'sudo /usr/local/sbin/warehouse-switch-network final')
        else:
            command = 'sudo /usr/local/sbin/warehouse-switch-network final'
        _, out, err = client.exec_command(command, timeout=45)
        out.channel.set_combine_stderr(True)
        text = out.read().decode('utf-8', errors='replace')
        code = out.channel.recv_exit_status()
        if code:
            raise RuntimeError(f'{role}: final-network preparation failed: {text[-1500:]}')
        print(f'{role}: final network prepared for next boot', flush=True)
    finally:
        client.close()


def main():
    proof = json.loads((PROJECT/'evidence/virtual-router-apply.json').read_text(encoding='utf-8'))
    output = proof.get('verification', {}).get('output', '')
    if not proof.get('passed') or not all(ip in output for ip in ('192.168.10.1', '192.168.20.1', '192.168.30.1')):
        raise RuntimeError('Real router apply verification is required first')
    for role in ROLES:
        snaps = vmrun('listSnapshots', role).stdout
        if 'warehouse-staging-ready-20261119' not in snaps:
            raise RuntimeError(f'{role}: staging snapshot missing')
    # Prepare every guest successfully before shutting any of them down.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(prepare, ROLES):
            pass
    for role in ROLES:
        client = connect(role)
        try:
            client.exec_command('sudo systemctl poweroff --no-block', timeout=8)
        finally:
            client.close()
    end = time.monotonic() + 120
    while True:
        listing = subprocess.run([str(VMRUN), 'list'], check=True, capture_output=True, text=True).stdout
        active = [role for role in ROLES if str(VMS/role/f'{role}.vmx') in listing]
        if not active:
            break
        if time.monotonic() >= end:
            raise RuntimeError('Guests still shutting down; no VMX was changed: ' + ', '.join(active))
        time.sleep(2)
    time.sleep(1)
    for role in ROLES:
        isolate_guest(role)
        print(f'{role}: sole NIC moved to wh-{role}; disk and snapshot pointers retained', flush=True)
    for role in ROLES:
        vmrun('start', role, 'nogui')
        print(f'{role}: booting in the isolated zone', flush=True)


if __name__ == '__main__':
    main()
