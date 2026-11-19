"""Guangzhou management connection only. No US endpoint or credentials here."""
import json
from pathlib import Path
import paramiko
try:
    from .runtime import PRIVATE
    from .lab_profile import load_profile
    from .lab_topology import MANAGEMENT_IP, MANAGEMENT_USER
except ImportError:
    from runtime import PRIVATE
    from lab_profile import load_profile
    from lab_topology import MANAGEMENT_IP, MANAGEMENT_USER


def connect_guangzhou(profile=None):
    profile = load_profile() if profile is None else profile
    config = paramiko.SSHConfig()
    with (Path.home()/'.ssh/config').open(encoding='utf-8') as f:
        config.parse(f)
    target = config.lookup(profile['cloud_ssh_alias'])
    if target.get('hostname') != MANAGEMENT_IP or target.get('user') != MANAGEMENT_USER:
        raise RuntimeError('Guangzhou management alias changed; do not guess another endpoint')
    known = target.get('userknownhostsfile')
    if not known or not Path(known).is_file():
        raise RuntimeError('Pinned Guangzhou host keys unavailable')
    client = paramiko.SSHClient()
    client.load_host_keys(known)
    try:
        client.connect(target['hostname'], port=int(target.get('port',22)), username=target['user'],
                       key_filename=target.get('identityfile'), look_for_keys=False, allow_agent=False,
                       timeout=10, auth_timeout=10, banner_timeout=10)
    except Exception:
        client.close()
        raise
    return client


def audit(client):
    command = """python3 - <<'PY'
import json,subprocess,hashlib,pathlib
units=['wg-quick@wg0.service','ssh.service','docker.service','qq-stack.service','qq-stack-firewall.service','qq-bridge.service','qq-dsh.service','qq-onebot-gateway.service']
state={}
for name in units:
 p=subprocess.run(['systemctl','show',name,'-p','ActiveState','-p','SubState'],capture_output=True,text=True)
 state[name]=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
def root(*args):
 return subprocess.check_output(['sudo','-n',*args])
hashes={}
for name in ['/etc/wireguard/wg0.conf','/etc/ssh/sshd_config']:
 try: hashes[name]=root('sha256sum',name).decode().split()[0]
 except subprocess.CalledProcessError: hashes[name]='unavailable'
chain=root('/usr/sbin/iptables','-S','QQ-STACK-IN')
print(json.dumps({'units':state,'config_sha256':hashes,'legacy_input_chain_sha256':hashlib.sha256(chain).hexdigest()}))
PY"""
    _, out, err = client.exec_command(command, timeout=30)
    data = out.read().decode()
    if out.channel.recv_exit_status():
        raise RuntimeError('Guangzhou read-only baseline audit failed')
    return json.loads(data)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-baseline', action='store_true',
                        help='First-install only: save the protected service baseline without replacing it')
    args = parser.parse_args(argv)
    client = connect_guangzhou()
    try:
        baseline = audit(client)
        if not args.capture_baseline:
            print(json.dumps(baseline, indent=2))
            return 0
        target = PRIVATE/'guangzhou-before.json'
        if target.exists():
            raise FileExistsError('Existing cloud baseline protected')
        target.write_text(json.dumps(baseline,indent=2),encoding='utf-8')
        print('Guangzhou legacy service/config baseline saved privately; no changes made')
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
