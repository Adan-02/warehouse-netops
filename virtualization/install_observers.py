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


def checked(relay,command,*,cloud=False,**kwargs):
    code,output=(relay.cloud_run if cloud else relay.run)(command,**kwargs)
    if code:
        raise RuntimeError('Owned observer operation failed (exit '+str(code)+'); inspect project-only logs')
    return output.strip()


def upload_code(relay,data,*,cloud=False):
    digest=hashlib.sha256(data).hexdigest()
    target=INSTALL+'/'+digest[:20]
    command='sudo -n sh -c '+shlex.quote(
        'set -eu; umask 077; '
        'test ! -L '+INSTALL+'; '
        'if [ -d '+INSTALL+' ]; then test "$(cat '+INSTALL+'/.owner)" = '+shlex.quote(OWNER)+'; '
        'else install -d -m 700 '+INSTALL+'; printf %s '+shlex.quote(OWNER)+' > '+INSTALL+'/.owner; fi; '
        'install -d -m 700 '+target+'; cat > '+target+'/code.tar.gz.part; '
        'test "$(sha256sum '+target+'/code.tar.gz.part | cut -d " " -f 1)" = '+digest+'; '
        'mv '+target+'/code.tar.gz.part '+target+'/code.tar.gz; '
        'tar --no-same-owner -xzf '+target+'/code.tar.gz -C '+target)
    checked(relay,command,cloud=cloud,input_data=data,timeout=60)
    return target


def install_role(relay,code_path,request,*,cloud=False):
    payload=json.dumps(request,separators=(',',':')).encode()
    request_path=code_path+'/request-'+request['role']+'.json'
    checked(relay,'sudo -n sh -c '+shlex.quote('umask 077; cat > '+request_path),cloud=cloud,input_data=payload,timeout=20)
    output=checked(relay,'sudo -n /usr/bin/python3 '+shlex.quote(code_path+'/ops/install_remote.py')+' --request '+shlex.quote(request_path),cloud=cloud,timeout=60)
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
        cloud_code=upload_code(relay,data,cloud=True)
        known=checked(relay,'sudo -n cat /var/lib/warehouse-netops-guangzhou/private/service_known_hosts',cloud=True)
        match=re.fullmatch(r'192\.168\.30\.10 (ssh-ed25519 [A-Za-z0-9+/=]+)',known)
        if not match:raise RuntimeError('Existing pinned service host key is unavailable')
        cloud=install_role(relay,cloud_code,{'role':'cloud','expected_peer':read_peer(PRIVATE/'router-wg.pub'),
            'service_host_public_key':match[1]},cloud=True)
        service_code=upload_code(relay,data)
        site=install_role(relay,service_code,{'role':'site','expected_peer':read_peer(PRIVATE/'guangzhou-wg.pub'),
            'router_host_public_key':router_host,'reporter_public_key':cloud['public_key']})
        upload(console,'/root/warehouse-observer-router.sh',router_script(site['public_key']),0o700)
        output=console.command('sh /root/warehouse-observer-router.sh',timeout=15)
        if output['exit_code']:raise RuntimeError('Router observer authorization failed')
    finally:
        console.close()
    # Only project-owned timers are enabled. The existing SSH, Docker, WG and QQ
    # units are neither restarted nor reconfigured by this installer.
    checked(relay,'sudo -n systemctl enable --now warehouse-observer-site.timer && sudo -n systemctl start warehouse-observer-site.service',timeout=40)
    checked(relay,'sudo -n systemctl enable --now warehouse-observer-cloud.timer && sudo -n systemctl start warehouse-observer-cloud.service',cloud=True,timeout=45)
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
        'webhook_enabled':False,'site_timer':site['timer'],'cloud_timer':cloud['timer'],
        'first_cycle_healthy':healthy,'first_cycle':observed}


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
            for cloud,role in ((False,'site'),(True,'cloud')):
                print(role+': '+checked(relay,'systemctl is-active warehouse-observer-'+role+'.timer',cloud=cloud))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
