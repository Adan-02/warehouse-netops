"""Install only the owned read-only observers; no network rules are changed.

`prepare` emits a private deterministic code bundle without remote actions.
`install` uses the already pinned Guangzhou/service maintenance transport and
local router COM1. Existing application/SSH/WireGuard services are not restarted.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import tarfile
import time
from datetime import datetime

PROJECT=Path(__file__).resolve().parents[1]
PRIVATE=PROJECT/'.secrets/virtual-lab'
INSTALL='/var/lib/warehouse-netops-observer-install'
OWNER='warehouse-netops-observer-installer-v1'


def public_key(value):
    if not isinstance(value,str) or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9@._-]+)?',value.strip()):
        raise ValueError('Expected an Ed25519 public key')
    return ' '.join(value.split()[:2])


def router_host_key(output):
    # The echoed sed expression also contains "ssh-ed25519 /p"; only accept
    # a complete output line, never a substring from the echoed command.
    matches=re.findall(r'(?:^|\r?\n)(ssh-ed25519 [A-Za-z0-9+/=]{68,})',output)
    if len(matches)!=1:raise RuntimeError('Router COM1 host pin unavailable')
    return public_key(matches[0])


def read_peer(path):
    value=Path(path).read_text(encoding='ascii').strip()
    data=base64.b64decode(value,validate=True)
    if len(data)!=32 or not any(data) or base64.b64encode(data).decode()!=value:
        raise ValueError('Expected a canonical WireGuard public key')
    return value


def bundle_bytes():
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w:gz') as archive:
        for path in sorted((PROJECT/'ops').iterdir()):
            if path.is_file() and path.suffix in ('.py','.sh'):
                if path.is_symlink():raise ValueError('Refusing source symlink')
                data=path.read_bytes()
                item=tarfile.TarInfo('ops/'+path.name)
                item.size=len(data);item.mode=0o644;item.mtime=0
                archive.addfile(item,io.BytesIO(data))
    return output.getvalue()


def target_connection(relay, target='site', cloud=None):
    """Use explicit locations internally; cloud is the existing call-site alias."""
    if cloud is not None:
        target='guangzhou' if cloud else 'site'
    if target not in ('site','guangzhou'):
        raise ValueError('Unknown observer target')
    return relay.cloud_run if target=='guangzhou' else relay.run


def checked(relay,command,*,target='site',cloud=None,**kwargs):
    code,output=target_connection(relay,target,cloud)(command,**kwargs)
    if code:
        raise RuntimeError('Owned observer operation failed (exit '+str(code)+'); inspect project-only logs')
    return output.strip()


def upload_code(relay,data,*,target='site',cloud=None):
    digest=hashlib.sha256(data).hexdigest()
    code_path=INSTALL+'/'+digest[:20]
    command='sudo -n sh -c '+shlex.quote(
        'set -eu; umask 077; '
        'test ! -L '+INSTALL+'; '
        'if [ -d '+INSTALL+' ]; then test "$(cat '+INSTALL+'/.owner)" = '+shlex.quote(OWNER)+'; '
        'else install -d -m 700 '+INSTALL+'; printf %s '+shlex.quote(OWNER)+' > '+INSTALL+'/.owner; fi; '
        'install -d -m 700 '+code_path+'; cat > '+code_path+'/code.tar.gz.part; '
        'test "$(sha256sum '+code_path+'/code.tar.gz.part | cut -d " " -f 1)" = '+digest+'; '
        'mv '+code_path+'/code.tar.gz.part '+code_path+'/code.tar.gz; '
        'tar --no-same-owner -xzf '+code_path+'/code.tar.gz -C '+code_path)
    checked(relay,command,target=target,cloud=cloud,input_data=data,timeout=60)
    return code_path


def install_role(relay,code_path,request,*,target='site',cloud=None):
    payload=json.dumps(request,separators=(',',':')).encode()
    request_path=code_path+'/request-'+request['role']+'.json'
    checked(relay,'sudo -n sh -c '+shlex.quote('umask 077; cat > '+request_path),target=target,cloud=cloud,input_data=payload,timeout=20)
    output=checked(relay,'sudo -n /usr/bin/python3 '+shlex.quote(code_path+'/ops/install_remote.py')+' --request '+shlex.quote(request_path),target=target,cloud=cloud,timeout=60)
    result=json.loads(output)
    if result.get('installed') is not True or result.get('role')!=request['role']:
        raise RuntimeError('Observer installer did not confirm the requested role')
    public_key(result.get('public_key',''))
    return result


def router_script(observer_public_key):
    public=public_key(observer_public_key)
    source=(PROJECT/'ops/router-read.sh').read_bytes()
    encoded=base64.b64encode(source).decode()
    line='command="/usr/libexec/warehouse-observer-read",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty '+public+' warehouse-site-observer'
    return ('#!/bin/sh\nset -eu\numask 077\n'
        'test ! -L /etc/dropbear/authorized_keys\n'
        'mkdir -p /usr/libexec /etc/dropbear\n'
        'printf %s '+shlex.quote(encoded)+' | base64 -d > /usr/libexec/warehouse-observer-read.new\n'
        'chmod 700 /usr/libexec/warehouse-observer-read.new\n'
        'mv /usr/libexec/warehouse-observer-read.new /usr/libexec/warehouse-observer-read\n'
        'touch /etc/dropbear/authorized_keys\n'
        'expected='+shlex.quote(line)+'\n'
        'existing=$(grep " warehouse-site-observer$" /etc/dropbear/authorized_keys || :)\n'
        'if [ -n "$existing" ]; then test "$existing" = "$expected"; '
        'else printf "\\n%s\\n" "$expected" >> /etc/dropbear/authorized_keys; fi\n'
        'chmod 600 /etc/dropbear/authorized_keys\n'
        'printf "observer router key installed\\n"\n').encode()


def install(relay,console_factory=None):
    if console_factory is None:
        try:
            from .console import Console
        except ImportError:
            from console import Console
        console_factory=Console
    try:
        from .console_files import upload
        from .cloud_connection import audit
    except ImportError:
        from console_files import upload
        from cloud_connection import audit
    baseline=json.loads((PRIVATE/'guangzhou-before.json').read_text(encoding='utf-8'))
    if audit(relay.client)!=baseline:
        raise RuntimeError('Protected Guangzhou baseline changed before observer installation')
    data=bundle_bytes()
    console=console_factory('router')
    try:
        output=console.command("dropbearkey -y -f /etc/dropbear/dropbear_ed25519_host_key | sed -n '/^ssh-ed25519 /p'",timeout=10)
        if output['exit_code']:raise RuntimeError('Router COM1 host pin unavailable')
        router_host=router_host_key(output['output'])
        guangzhou_code=upload_code(relay,data,target='guangzhou')
        known=checked(relay,'sudo -n cat /var/lib/warehouse-netops-guangzhou/private/service_known_hosts',target='guangzhou')
        match=re.fullmatch(r'192\.168\.30\.10 (ssh-ed25519 [A-Za-z0-9+/=]+)',known)
        if not match:raise RuntimeError('Existing pinned service host key is unavailable')
        # The systemd/installer role remains "cloud" for existing installations.
        guangzhou=install_role(relay,guangzhou_code,{'role':'cloud','expected_peer':read_peer(PRIVATE/'router-wg.pub'),
            'service_host_public_key':match[1]},target='guangzhou')
        service_code=upload_code(relay,data)
        site=install_role(relay,service_code,{'role':'site','expected_peer':read_peer(PRIVATE/'guangzhou-wg.pub'),
            'router_host_public_key':router_host,'reporter_public_key':guangzhou['public_key']})
        upload(console,'/root/warehouse-observer-router.sh',router_script(site['public_key']),0o700)
        output=console.command('sh /root/warehouse-observer-router.sh',timeout=15)
        if output['exit_code']:raise RuntimeError('Router observer authorization failed')
    finally:
        console.close()
    # Only project-owned timers are enabled. The existing SSH, Docker, WG and QQ
    # units are neither restarted nor reconfigured by this installer.
    checked(relay,'sudo -n systemctl enable --now warehouse-observer-site.timer && sudo -n systemctl start warehouse-observer-site.service',timeout=40)
    checked(relay,'sudo -n systemctl enable --now warehouse-observer-cloud.timer && sudo -n systemctl start warehouse-observer-cloud.service',target='guangzhou',timeout=45)
    unchanged=audit(relay.client)==baseline
    if not unchanged:raise RuntimeError('Protected Guangzhou baseline changed after installation')
    observed={}
    for name,identity in (('local','site'),('remote','guangzhou')):
        value=json.loads(checked(relay,'sudo -n cat /var/lib/warehouse-netops-observer/observations/'+name+'.json'))
        if value.get('observer_id')!=identity:raise RuntimeError('Installed observer returned the wrong identity')
        observed[name]={key:row.get('state') for key,row in value.get('components',{}).items()}
    healthy=bool(observed['local']) and bool(observed['remote']) and all(state=='healthy' for rows in observed.values() for state in rows.values())
    return {'installed':True,'bundle_sha256':hashlib.sha256(data).hexdigest(),
        'protected_guangzhou_baseline_unchanged':unchanged,'network_rules_changed':False,'us_accessed':False,
        'webhook_enabled':False,'site_timer':site['timer'],'cloud_timer':guangzhou['timer'],
        'first_cycle_healthy':healthy,'first_cycle':observed}


def summarize_cycle(value, target, now):
    """Expose only stage enums and timestamps, never arbitrary persisted text."""
    result={'state':'unknown','reason_code':'cycle_unavailable','observed_at':None,'stages':{}}
    if not isinstance(value,dict):
        return result
    if value.get('observer_id',target)!=target:
        result['reason_code']='observer_identity_mismatch'
        return result
    stamp=value.get('observed_at')
    try:
        observed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
        if observed.tzinfo is None:raise ValueError('Timezone required')
        age=now-observed.timestamp()
    except (TypeError,ValueError,AttributeError):
        return result
    result['observed_at']=observed.isoformat()
    stages=value.get('stages',{})
    if not isinstance(stages,dict):stages={}
    allowed_reasons={'ok','recorded','accepted','receiver_rejected','report_transport_failed',
        'synchronized','event_http_not_200','event_sync_failed','webhook_disabled','delivery_failed',
        'retry_pending','completed','dispatch_failed','backup_failed','notification_state_unavailable',
        'service_unreachable','wireguard_unavailable','router_observation_unavailable','router_network_unhealthy'}
    for name in ('probe','reachability','report','event_sync','dispatch','maintenance'):
        row=stages.get(name,{})
        if not isinstance(row,dict):continue
        state=row.get('state')
        if state not in ('healthy','unhealthy','unknown','skipped','disabled'):continue
        reason=row.get('reason_code')
        if not isinstance(reason,str) or reason not in allowed_reasons:reason='invalid_diagnostic'
        success=None
        try:
            at=datetime.fromisoformat(row.get('last_success_at').replace('Z','+00:00'))
            if at.tzinfo is not None:success=at.isoformat()
        except (TypeError,ValueError,AttributeError):pass
        result['stages'][name]={'state':state,'reason_code':reason,'last_success_at':success}
    if not -5<=age<=35:
        result['reason_code']='cycle_stale'
        return result
    required={'probe'} if target=='site' else {'probe','reachability','report','event_sync','dispatch','maintenance'}
    if not required.issubset(result['stages']):
        result['reason_code']='legacy_diagnostics_missing'
        return result
    states={result['stages'][name]['state'] for name in required}
    result['state']='unhealthy' if 'unhealthy' in states else 'unknown' if 'unknown' in states else 'healthy'
    result['reason_code']='ok' if result['state']=='healthy' else 'cycle_degraded'
    return result


def observer_status(relay, target, clock=time.time):
    """Read timers and bounded cycle diagnostics; no installation or probes."""
    connection=target_connection(relay,target)
    role='cloud' if target=='guangzhou' else 'site'
    unit='warehouse-observer-'+role+'.timer'
    code,output=connection('systemctl is-active '+unit,timeout=10)
    timer_state=output.strip() if output.strip() in ('active','inactive','failed','activating','deactivating') else 'unknown'
    files=(['/var/lib/warehouse-netops-observer/observations/site-status.json',
        '/var/lib/warehouse-netops-observer/observations/local.json'] if target=='site'
        else ['/var/lib/warehouse-netops-observer/private/status.json'])
    script="""import json,pathlib,stat
value={}
for name in %r:
 try:
  path=pathlib.Path(name);info=path.lstat()
  if stat.S_ISREG(info.st_mode) and info.st_size<=32768:
   value=json.loads(path.read_text());break
 except (OSError,ValueError):pass
print(json.dumps(value))
""" % files
    try:
        status_code,text=connection('sudo -n /usr/bin/python3 -c '+shlex.quote(script),timeout=10,tail_limit=32769)
        value=json.loads(text) if status_code==0 and len(text.encode())<=32768 else {}
    except (OSError,TimeoutError,ValueError):
        value={}
    return {'observer_id':target,'timer':{'unit':unit,'state':timer_state},
        'observation':summarize_cycle(value,target,clock())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','install','status'))
    parser.add_argument('--output',type=Path,default=PRIVATE/'observer-code.tar.gz')
    args=parser.parse_args()
    if args.action=='prepare':
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_bytes(bundle_bytes());args.output.chmod(0o600)
        print(json.dumps({'prepared':True,'bundle':str(args.output),'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest()}))
        return 0
    try:
        from .release_transport import ServiceRelay
    except ImportError:
        from release_transport import ServiceRelay
    with ServiceRelay() as relay:
        if args.action=='install':
            report=install(relay)
            (PROJECT/'evidence/observer-installation.local.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report))
        else:
            for target in ('site','guangzhou'):
                print(json.dumps(observer_status(relay,target)))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
