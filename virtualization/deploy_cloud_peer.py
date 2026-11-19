"""Prepare/deploy only the independent Guangzhou WireGuard endpoint.

Default invocation prepares private artifacts and performs a read-only audit.
The --install path additionally requires a real, passing local network matrix.
No US address, credential or connection is used by this module.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from cloud_connection import audit, connect_guangzhou
from guangzhou_peer import generate_bundle, read_key
from provision_guests import exec_collect
from runtime import PRIVATE, PROJECT


def write_private(path: Path, data: bytes) -> None:
    with path.open('xb') as stream:
        stream.write(data)
    path.chmod(0o600)


def keypair(name: str) -> None:
    secret = PRIVATE / (name + '.key')
    public = PRIVATE / (name + '.pub')
    if secret.exists():
        private = X25519PrivateKey.from_private_bytes(base64.b64decode(read_key(secret, name)))
    else:
        if public.exists():
            raise RuntimeError('Existing public key has no matching private key; refusing replacement')
        private = X25519PrivateKey.generate()
        raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
        write_private(secret, base64.b64encode(raw) + b'\n')
    derived = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode('ascii')
    if public.exists():
        if read_key(public, name + ' public') != derived:
            raise RuntimeError('Existing WireGuard private/public key pair does not match')
    else:
        write_private(public, derived.encode('ascii') + b'\n')


def prepare() -> tuple[Path, dict]:
    PRIVATE.mkdir(parents=True, exist_ok=True)
    keypair('router-wg')
    keypair('guangzhou-wg')
    metadata = PRIVATE / 'guangzhou-deploy-state.json'
    if metadata.exists():
        state = json.loads(metadata.read_text(encoding='utf-8'))
        bundle = Path(state['local_bundle'])
        if not bundle.is_dir() or bundle.parent.resolve() != PRIVATE.resolve():
            raise RuntimeError('Saved private cloud bundle path is invalid')
        manifest = json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
        if manifest['bundle_id'] != state['bundle_id']:
            raise RuntimeError('Saved bundle identity changed')
        return bundle, state
    identifier = uuid.uuid4().hex
    bundle = PRIVATE / ('guangzhou-bundle-' + identifier)
    generate_bundle(bundle, PRIVATE/'guangzhou-wg.key', PRIVATE/'router-wg.pub',
                    wan_interface='eth0', bundle_id=identifier)
    state = {'bundle_id': identifier, 'local_bundle': str(bundle),
             'remote_bundle': '/root/warehouse-netops-deploy/' + identifier}
    write_private(metadata, (json.dumps(state, indent=2)+'\n').encode('utf-8'))
    return bundle, state


def require_matrix() -> None:
    report = json.loads((PROJECT/'evidence/virtual-matrix.json').read_text(encoding='utf-8'))
    if report.get('phase') != 'baseline' or report.get('passed') is not True:
        raise RuntimeError('A passing real local access matrix is required before cloud changes')


def remote_command(client, command, timeout=60):
    code, output = exec_collect(client, command, timeout=timeout, tail_limit=12000)
    if code:
        raise RuntimeError('Guangzhou project command failed: ' + output[-2500:])
    return output


def upload_bundle(client, bundle: Path, state: dict) -> None:
    target = state['remote_bundle']
    qtarget = shlex.quote(target)
    # Existing root-owned bundles must be byte-identical and are never replaced.
    exists_code, _ = exec_collect(client, 'sudo -n test -d ' + qtarget, timeout=10)
    if exists_code == 0:
        verify_uploaded_manifest(client, bundle, target)
        remote_command(client, 'sudo -n sh '+qtarget+'/install.sh check --project-dir '+qtarget)
        return
    if exists_code != 1:
        raise RuntimeError('Cannot inspect the existing private remote bundle path')
    sftp = client.open_sftp()
    stage = None
    try:
        home = sftp.normalize('.')
        if home != '/home/qqops':
            raise RuntimeError('Unexpected Guangzhou upload account home')
        stage = home + '/.warehouse-upload-' + state['bundle_id']
        sftp.mkdir(stage, mode=0o700)
        sftp.chmod(stage, 0o700)
        for path in bundle.iterdir():
            if not path.is_file() or path.is_symlink():
                raise RuntimeError('Private cloud bundle contains an unexpected entry')
            destination = stage + '/' + path.name
            sftp.put(str(path), destination)
            sftp.chmod(destination, 0o700 if path.suffix == '.sh' else 0o600)
    finally:
        sftp.close()
    qstage = shlex.quote(stage)
    command = ('sudo -n sh -c ' + shlex.quote(
        'set -eu; umask 077; test ! -e '+qtarget+'; '
        'test ! -L /root/warehouse-netops-deploy; '
        'install -d -m 700 /root/warehouse-netops-deploy; '
        'chown -R root:root '+qstage+'; mv -- '+qstage+' '+qtarget+'; '
        'test "$(stat -c %a '+qtarget+')" = 700'))
    remote_command(client, command)
    verify_uploaded_manifest(client, bundle, target)


def verify_uploaded_manifest(client, bundle: Path, target: str) -> None:
    expected = hashlib.sha256((bundle/'checksums.txt').read_bytes()).hexdigest()
    observed = remote_command(client, 'sudo -n sha256sum '+shlex.quote(target+'/checksums.txt')).split()
    if not observed or observed[0] != expected:
        raise RuntimeError('Uploaded cloud bundle checksum manifest differs from the prepared bundle')


def status(client):
    command = """sudo -n python3 - <<'PY'
import json,subprocess
def run(*a):
 p=subprocess.run(a,capture_output=True,text=True)
 return {'exit_code':p.returncode,'output':p.stdout.strip()}
rows={name:run('systemctl','is-active',name) for name in ['wg-quick@wg-warehouse.service','warehouse-wg-firewall.service','warehouse-wg-reconcile.timer']}
handshake=run('wg','show','wg-warehouse','latest-handshakes')
times=[int(line.split()[-1]) for line in handshake['output'].splitlines() if line.split()]
print(json.dumps({'units':rows,'listen_port':run('wg','show','wg-warehouse','listen-port'),'routes':run('ip','-4','route','show','dev','wg-warehouse'),'latest_handshake_epoch':max(times,default=0),'firewall_check':run('sh','/usr/local/lib/warehouse-netops-guangzhou/firewall.sh','check')}))
PY"""
    return json.loads(remote_command(client, command, timeout=30))


def install(client, bundle, state):
    original = json.loads((PRIVATE/'guangzhou-before.json').read_text(encoding='utf-8'))
    before = audit(client)
    if before != original:
        raise RuntimeError('Guangzhou protected baseline changed before installation; inspect before proceeding')
    write_path = PRIVATE/'guangzhou-deploy-audit-before.json'
    if not write_path.exists():
        write_private(write_path, (json.dumps(before,indent=2)+'\n').encode('utf-8'))
    upload_bundle(client, bundle, state)
    target = shlex.quote(state['remote_bundle'])
    prefix = 'sudo -n sh '+target+'/install.sh '
    report = {'observed_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Guangzhou independent WireGuard endpoint only', 'us_accessed': False,
              'interface': 'wg-warehouse', 'udp_port': 51830, 'bundle_id': state['bundle_id'],
              'cloud_security_group_changed': False, 'preflight_passed': False,
              'installed': False, 'rollback_attempted': False}
    installing = False
    try:
        remote_command(client, prefix+'check --project-dir '+target)
        report['preflight_passed'] = True
        installing = True
        remote_command(client, prefix+'install --project-dir '+target, timeout=180)
        observed = status(client)
        after = audit(client)
        report['legacy_baseline_unchanged'] = before == after == original
        report['status'] = observed
        report['installed'] = (report['legacy_baseline_unchanged'] and
            all(row['exit_code'] == 0 for row in observed['units'].values()) and
            observed['listen_port'] == {'exit_code':0,'output':'51830'} and
            observed['firewall_check']['exit_code'] == 0)
        if not report['installed']:
            raise RuntimeError('Guangzhou independent endpoint or protected baseline verification failed')
        report['reachability'] = 'not_yet_verified_router_or_cloud_security_group'
        return report
    except Exception as error:
        report['error_type'] = type(error).__name__
        # Diagnostic tails from the project installer contain no key material;
        # keep detailed exception text private, not in the public evidence file.
        private_error = PRIVATE/'guangzhou-deploy-error.txt'
        private_error.write_text(str(error),encoding='utf-8')
        if installing:
            report['rollback_attempted'] = True
            code, output = exec_collect(client, prefix+'rollback --project-dir '+target, timeout=120)
            report['rollback_succeeded'] = code == 0
            if code:
                with private_error.open('a',encoding='utf-8') as stream:
                    stream.write('\nRollback: '+output)
        report['legacy_baseline_unchanged_after_failure'] = audit(client) == original
        raise
    finally:
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        (PROJECT/'evidence/guangzhou-deployment.json').write_text(
            json.dumps(report,indent=2)+'\n',encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    if args.install:
        require_matrix()
    bundle, state = prepare()
    client = connect_guangzhou()
    try:
        if args.install:
            report = install(client,bundle,state)
            print(json.dumps({'installed':report['installed'],
                              'legacy_baseline_unchanged':report['legacy_baseline_unchanged'],
                              'listen_port':51830,'reachability':report['reachability']}))
        else:
            original = json.loads((PRIVATE/'guangzhou-before.json').read_text(encoding='utf-8'))
            observed = audit(client)
            if observed != original:
                raise RuntimeError('Guangzhou protected baseline differs; no changes made')
            print('Private WireGuard key pairs and bundle prepared. Guangzhou read-only audit matches baseline. No cloud changes made.')
    finally:
        client.close()


if __name__ == '__main__':
    main()
