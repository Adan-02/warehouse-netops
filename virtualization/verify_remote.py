"""Verify Guangzhou's dedicated VPN, HTTP and pinned key-only SSH path."""
import base64
from datetime import datetime, timezone
import json
import re
import shlex

from cloud_connection import connect_guangzhou, audit
from provision_guests import exec_collect
from check_matrix import MatrixRunner, encoded_python, extract_json, deny_counter_evidence, network_ready
from check_faults import http_script, online, recent_handshake
from console import Console
from runtime import PROJECT, PRIVATE

STATE = '/var/lib/warehouse-netops-guangzhou/private'
EXPECTED_ALLOWED_IPS = ['10.89.0.2/32', '192.168.30.10/32']


def checked(client, command, timeout=30):
    code, output = exec_collect(client, command, timeout=timeout, tail_limit=16000)
    if code:
        raise RuntimeError('Dedicated Guangzhou verification command failed')
    return output


def expectations():
    state = json.loads((PRIVATE/'guangzhou-deploy-state.json').read_text(encoding='utf-8'))
    identifier = state.get('bundle_id', '')
    if not re.fullmatch('[0-9a-f]{32}', identifier):
        raise RuntimeError('Saved Guangzhou bundle identity is invalid')
    bundle = PRIVATE/('guangzhou-bundle-'+identifier)
    owner = (bundle/'.project-marker').read_text(encoding='utf-8').strip()
    if owner != 'warehouse-netops-guangzhou-v1:'+identifier:
        raise RuntimeError('Saved Guangzhou ownership marker does not match the bundle')
    peer = (PRIVATE/'router-wg.pub').read_text(encoding='ascii').strip()
    try:
        raw = base64.b64decode(peer, validate=True)
    except ValueError:
        raise RuntimeError('Expected router public key is invalid') from None
    if len(raw) != 32 or not any(raw) or base64.b64encode(raw).decode() != peer:
        raise RuntimeError('Expected router public key is invalid')
    return owner, peer


def private_guard_source(owner):
    """Shared remote guard; it never reads a private key into its output."""
    return f"""import os,stat,subprocess,tempfile
from pathlib import Path
root=Path('/var/lib/warehouse-netops-guangzhou')
private=root/'private'
def guarded(path,directory=False):
 info=path.lstat()
 expected=0o700 if directory else 0o600
 kind=stat.S_ISDIR if directory else stat.S_ISREG
 if not kind(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=expected:
  raise RuntimeError('Unsafe project-owned private path')
for directory in (root,private):guarded(directory,True)
guarded(root/'.owner')
if (root/'.owner').read_text().strip()!={owner!r}:
 raise RuntimeError('Guangzhou private directory ownership marker differs')
os.umask(0o077)
"""


def maintenance_key_script(owner):
    return private_guard_source(owner) + """key=private/'maintenance_ed25519'
pub=private/'maintenance_ed25519.pub'
if key.is_symlink() or pub.is_symlink():
 raise RuntimeError('Refusing maintenance key symlinks')
if key.exists():
 guarded(key);guarded(pub)
else:
 if pub.exists():raise RuntimeError('Orphan maintenance public key exists')
 subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-C','warehouse-guangzhou-maintenance','-f',str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 key.chmod(0o600);pub.chmod(0o600)
derived=' '.join(subprocess.check_output(['ssh-keygen','-y','-f',str(key)],text=True).split()[:2])
if pub.read_text().split()[:2]!=derived.split():
 raise RuntimeError('Existing maintenance private/public keys differ')
guarded(key);guarded(pub)
print(derived,flush=True)
"""


def host_pin_script(owner, pinned):
    return private_guard_source(owner) + f"""target=private/'service_known_hosts'
value={pinned!r}
if target.is_symlink():raise RuntimeError('Refusing known-hosts symlink')
if target.exists():
 guarded(target)
 if target.read_text()!=value:raise RuntimeError('Existing service host pin differs from COM1')
else:
 with target.open('x') as stream:stream.write(value)
 target.chmod(0o600)
guarded(target)
if target.read_text()!=value:raise RuntimeError('Service host pin read-back failed')
print('dedicated service host pin verified',flush=True)
"""


def prepare_ssh(client, service, owner):
    public = checked(client, 'sudo -n '+encoded_python(maintenance_key_script(owner))).strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+', public):
        raise RuntimeError('Dedicated maintenance public key is invalid')
    # COM1 supplies the host identity independently of the network being tested.
    result = service.command('cat /etc/ssh/ssh_host_ed25519_key.pub')
    matches = re.findall(r'(ssh-ed25519 [A-Za-z0-9+/=]+)', result['output'])
    if result['exit_code'] or len(matches) != 1:
        raise RuntimeError('Service host key not available unambiguously through local COM1')
    checked(client, 'sudo -n '+encoded_python(host_pin_script(owner, '192.168.30.10 '+matches[0]+'\n')))
    script = """from pathlib import Path
import os,tempfile
p=Path.home()/'.ssh'
if p.is_symlink():raise RuntimeError('Unsafe SSH directory')
p.mkdir(mode=0o700,exist_ok=True);p.chmod(0o700)
f=p/'authorized_keys'
if f.is_symlink():raise RuntimeError('Unsafe authorized_keys path')
line=%r
old=f.read_text() if f.exists() else ''
parts=line.split()
present=any(any(row.split()[i:i+2]==parts for i in range(len(row.split())-1)) for row in old.splitlines())
if not present:
 with tempfile.NamedTemporaryFile(mode='w',dir=p,delete=False) as stream:
  stream.write(old.rstrip()+'\\n'+line+' warehouse-guangzhou-maintenance\\n');temporary=Path(stream.name)
 temporary.chmod(0o600);os.replace(temporary,f)
f.chmod(0o600)
if line.split()[:2] not in [row.split()[:2] for row in f.read_text().splitlines()] and not present:
 raise RuntimeError('Maintenance authorization read-back failed')
print('dedicated maintenance public key present')
""" % public
    if service.command(encoded_python(script))['exit_code']:
        raise RuntimeError('Could not install dedicated maintenance public key')


def path_script():
    return """import subprocess,json,time,re
def run(*args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=5)
 return p.returncode,p.stdout.strip()
r={}
for field,args in [('route',('ip','-j','-4','route','get','192.168.30.10','from','10.89.0.1')),('peers',('sudo','-n','wg','show','wg-warehouse','peers')),('allowed',('sudo','-n','wg','show','wg-warehouse','allowed-ips')),('handshakes',('sudo','-n','wg','show','wg-warehouse','latest-handshakes'))]:
 code,text=run(*args);r[field+'_exit']=code
 if field=='route':r['routes']=json.loads(text) if code==0 else []
 elif field=='peers':r['peers']=text.splitlines()
 elif field=='allowed':r['allowed']=[{'peer':line.split()[0],'ips':[v for v in re.split(r'[,\\s]+',line.split(None,1)[1]) if v]} for line in text.splitlines() if len(line.split(None,1))==2]
 else:r['handshakes']=[{'peer':line.split()[0],'epoch':int(line.split()[1])} for line in text.splitlines() if len(line.split())==2]
r['observed_at']=time.time()
print('\\nWH_REMOTE_PATH_'+json.dumps(r),flush=True)
"""


def path_ready(value, expected_peer):
    routes = value.get('routes', [])
    peers = value.get('peers', [])
    allowed = value.get('allowed', [])
    return (
        all(value.get(field+'_exit') == 0 for field in ('route','peers','allowed','handshakes'))
        and len(routes) == 1 and routes[0].get('dev') == 'wg-warehouse'
        and routes[0].get('from', '10.89.0.1') == '10.89.0.1'
        and peers == [expected_peer] and len(allowed) == 1
        and allowed[0].get('peer') == expected_peer
        and sorted(allowed[0].get('ips', [])) == EXPECTED_ALLOWED_IPS
        and len(value.get('handshakes', [])) == 1
        and value['handshakes'][0].get('peer') == expected_peer
    )


def expected_handshake(value, expected_peer):
    rows = value.get('handshakes', [])
    if len(rows) != 1 or rows[0].get('peer') != expected_peer:
        return {'wireguard': {}}
    return {'wireguard': {'command_exit':value.get('handshakes_exit'),
                          'latest_handshake':rows[0].get('epoch',0),
                          'observed_at':value.get('observed_at',0)}}


def ssh_identity_matches(code, output):
    return code == 0 and output.strip().splitlines() == ['1000','warehouse','warehouse-service']


def main():
    runner = None
    client = None
    original = None
    report = {'observed_at':datetime.now(timezone.utc).isoformat(),'scope':'Guangzhou independent WireGuard to isolated service VM','us_accessed':False,'checks':[]}
    def add(name, passed, evidence):
        report['checks'].append({'name':name,'status':'pass' if passed else 'failed','evidence':evidence})
    try:
        original = json.loads((PRIVATE/'guangzhou-before.json').read_text(encoding='utf-8'))
        owner, expected_peer = expectations()
        client = connect_guangzhou()
        runner = MatrixRunner(Console)
        initial_unchanged = audit(client) == original
        add('guangzhou_legacy_baseline_before',initial_unchanged,{'unchanged':initial_unchanged})
        if not initial_unchanged:
            raise RuntimeError('Guangzhou legacy baseline changed before verification')
        inventory = runner.guest('service','inventory')
        listening = inventory['self_connect']
        add('service_source_and_single_nic',network_ready('service',inventory),inventory)
        add('target_ports_listening',all(listening.get(str(p)) is True for p in (22,1883,8080)),listening)
        before = runner.router()
        add('router_fw4_before',before['fw4_check_exit']==0,{'exit_code':before['fw4_check_exit']})
        script = """import socket,json
r={'ports':{}}
for port in (22,1883,8080):
 s=socket.socket();s.settimeout(3);row={'connected':False}
 try:
  s.bind(('10.89.0.1',0));row['source_ip']=s.getsockname()[0]
  s.connect(('192.168.30.10',port));row['connected']=True
 except OSError as e:
  row['error_type']=type(e).__name__;row['errno']=e.errno
 finally:s.close()
 r['ports'][str(port)]=row
print('\\nWH_REMOTE_PORTS_'+json.dumps(r))
"""
        result = extract_json(checked(client,encoded_python(script)), 'WH_REMOTE_PORTS_')
        after = runner.router()
        add('router_fw4_after',after['fw4_check_exit']==0,{'exit_code':after['fw4_check_exit']})
        denied = deny_counter_evidence(before['rules'],after['rules'],'vpn')
        report['vpn_deny_counter'] = denied
        for port in (22,1883,8080):
            row = result['ports'][str(port)]
            passed = row.get('source_ip')=='10.89.0.1' and row.get('connected') is (port!=1883)
            if port==1883:
                passed = passed and denied['verified'] and listening['1883']
            add('guangzhou_to_service_'+str(port),passed,row)
        http = extract_json(checked(client,encoded_python(http_script('WH_REMOTE_HTTP_','10.89.0.1'))), 'WH_REMOTE_HTTP_')
        add('guangzhou_dashboard_fresh',online(http,'10.89.0.1'),http)
        path = extract_json(checked(client,encoded_python(path_script())), 'WH_REMOTE_PATH_')
        add('independent_wireguard_route_and_peer',path_ready(path,expected_peer),path)
        handshake = expected_handshake(path,expected_peer)
        add('independent_wireguard_handshake',recent_handshake(handshake),handshake['wireguard'])
        if all(row['status']=='pass' for row in report['checks']):
            prepare_ssh(client,runner.console('service'),owner)
            report['ssh_preparation'] = 'Dedicated key and COM1 host pin persist in project private state; only its public key is authorized on the service VM.'
            command = ('sudo -n ssh -F /dev/null -i '+STATE+'/maintenance_ed25519 -b 10.89.0.1 '
                '-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes '
                '-o UserKnownHostsFile='+STATE+'/service_known_hosts -o GlobalKnownHostsFile=/dev/null '
                '-o IdentityAgent=none -o IdentitiesOnly=yes -o PreferredAuthentications=publickey '
                '-o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o HostKeyAlgorithms=ssh-ed25519 '
                'warehouse@192.168.30.10 '+shlex.quote('id -u; id -un; hostname'))
            code, output = exec_collect(client,command,timeout=15,tail_limit=2000)
            add('guangzhou_pinned_key_ssh',ssh_identity_matches(code,output),{'exit_code':code,'output':output.strip()})
    except Exception as error:
        add('remote_verification_completed',False,{'error_type':type(error).__name__})
    finally:
        try:
            if client is not None and original is not None:
                try:
                    unchanged = audit(client)==original
                    add('guangzhou_legacy_baseline_unchanged',unchanged,{'unchanged':unchanged})
                except Exception as error:
                    add('guangzhou_legacy_baseline_unchanged',False,{'error_type':type(error).__name__})
        finally:
            try:
                if runner is not None:
                    runner.close()
            except Exception as error:
                add('local_consoles_closed',False,{'error_type':type(error).__name__})
            finally:
                try:
                    if client is not None:
                        client.close()
                except Exception as error:
                    add('guangzhou_connection_closed',False,{'error_type':type(error).__name__})
    report['passed'] = bool(report['checks']) and all(row['status']=='pass' for row in report['checks'])
    try:
        (PROJECT/'evidence/virtual-remote.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    except Exception as error:
        add('evidence_written',False,{'error_type':type(error).__name__})
        report['passed'] = False
    print(json.dumps({'passed':report['passed'],'checks':[{k:r[k] for k in ('name','status')} for r in report['checks']]}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
