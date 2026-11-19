"""Root-only installation of this project's independent observer units."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

try:
    import grp
    import pwd
except ImportError:  # The renderer/tests also run on Windows; installation does not.
    grp=pwd=None

OWNER='warehouse-netops-observer-v1'
BASE=Path('/var/lib/warehouse-netops-observer')
PRIVATE=BASE/'private'
CODE=Path('/opt/warehouse-netops-observer')
USER='warehouse-observer'


def run(*args):
    return subprocess.check_output(args,stderr=subprocess.PIPE,text=True,timeout=20)


def write(path,data,mode=0o644,uid=0,gid=0):
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink():raise RuntimeError('Unsafe project installation path')
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
        temporary=Path(stream.name)
        stream.write(data.encode() if isinstance(data,str) else data)
    temporary.chmod(mode)
    os.chown(temporary,uid,gid)
    os.replace(temporary,path)


def setup():
    if pwd is None or grp is None or not hasattr(os,'geteuid') or os.geteuid()!=0:raise RuntimeError('Project installation requires a root Linux session')
    if BASE.is_symlink() or CODE.is_symlink():raise RuntimeError('Unsafe project root')
    if BASE.exists() and not (BASE/'.owner').exists():
        if set(p.name for p in BASE.iterdir())-{'observations'}:
            raise RuntimeError('Project state has no matching owner marker')
    if (BASE/'.owner').exists():
        if (BASE/'.owner').is_symlink() or (BASE/'.owner').read_text().strip()!=OWNER:
            raise RuntimeError('Project state owner differs')
    if CODE.exists() and not (CODE/'.owner').exists():raise RuntimeError('Existing code path is not owned by this installer')
    if (CODE/'.owner').exists() and (CODE/'.owner').read_text().strip()!=OWNER:raise RuntimeError('Existing code marker differs')
    BASE.mkdir(parents=True,exist_ok=True);BASE.chmod(0o755)
    write(BASE/'.owner',OWNER+'\n',0o600)
    try:
        account=pwd.getpwnam(USER)
        if account.pw_dir!=str(PRIVATE) or account.pw_shell!='/bin/sh':raise RuntimeError('Existing observer account differs')
    except KeyError:
        run('useradd','--system','--user-group','--home-dir',str(PRIVATE),'--shell','/bin/sh',USER)
        account=pwd.getpwnam(USER)
    if account.pw_uid==0 or set(grp.getgrgid(g).gr_name for g in os.getgrouplist(USER,account.pw_gid))&{'sudo','docker'}:
        raise RuntimeError('Observer account has unexpected administrative privileges')
    PRIVATE.mkdir(exist_ok=True);PRIVATE.chmod(0o700);os.chown(PRIVATE,account.pw_uid,account.pw_gid)
    observations=BASE/'observations'
    if observations.is_symlink():raise RuntimeError('Unsafe observations directory')
    observations.mkdir(exist_ok=True);observations.chmod(0o755);os.chown(observations,account.pw_uid,account.pw_gid)
    CODE.mkdir(exist_ok=True);CODE.chmod(0o755)
    write(CODE/'.owner',OWNER+'\n',0o600)
    source=Path(__file__).resolve().parent
    for item in source.iterdir():
        if item.is_file() and item.suffix in ('.py','.sh'):
            write(CODE/'ops'/item.name,item.read_bytes(),0o644)
    return account


def check_public(value):
    if not isinstance(value,str) or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9@._-]+)?',value):
        raise RuntimeError('Invalid project SSH public key')
    return ' '.join(value.split()[:2])


def check_peer(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9+/]{43}=',value):raise RuntimeError('Invalid expected WireGuard peer')
    return value


def keypair(name,account):
    path=PRIVATE/name
    pub=Path(str(path)+'.pub')
    if path.is_symlink() or pub.is_symlink():raise RuntimeError('Refusing observer key symlink')
    if not path.exists():
        if pub.exists():raise RuntimeError('Orphan observer public key exists')
        run('runuser','-u',USER,'--','ssh-keygen','-q','-t','ed25519','-N','','-C','warehouse-observer','-f',str(path))
    for item in (path,pub):
        info=item.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=account.pw_uid:raise RuntimeError('Observer key ownership differs')
        item.chmod(0o600)
    derived=check_public(run('runuser','-u',USER,'--','ssh-keygen','-y','-f',str(path)).strip())
    if check_public(pub.read_text().strip())!=derived:raise RuntimeError('Observer private/public keys do not match')
    return derived


def pin(name,address,public,account):
    expected=address+' '+check_public(public)+'\n'
    path=PRIVATE/name
    if path.exists() and (path.is_symlink() or path.read_text()!=expected):raise RuntimeError('Existing observer host pin differs')
    write(path,expected,0o600,account.pw_uid,account.pw_gid)


def configuration(role,peer,account):
    path=PRIVATE/'config.json'
    if path.is_symlink():raise RuntimeError('Unsafe observer configuration')
    value=json.loads(path.read_text()) if path.exists() else {}
    value.update({'expected_peer':check_peer(peer),'state_dir':str(PRIVATE)})
    if role=='site':value['observations_dir']=str(BASE/'observations')
    else:value.setdefault('webhook',{'enabled':False,'url':'','secret':'','allow_loopback_http_for_tests':False})
    write(path,json.dumps(value,indent=2)+'\n',0o600,account.pw_uid,account.pw_gid)


def unit(role):
    cloud=role=='cloud'
    code=CODE.as_posix();private=PRIVATE.as_posix();observations=(BASE/'observations').as_posix()
    return f'''[Unit]
Description=Warehouse {'Guangzhou' if cloud else 'site'} read-only observations
After=network-online.target{' wg-quick@wg-warehouse.service' if cloud else ''}
Wants=network-online.target

[Service]
Type=oneshot
User={USER}
Group={USER}
Environment=PYTHONPATH={code}
WorkingDirectory={code}
ExecStart=/usr/bin/python3 -m ops.observer {role} --config {private}/config.json --once
TimeoutStartSec=30
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={private if cloud else observations}
{'NoNewPrivileges=yes' if not cloud else '# Only the fixed zero-argument cloud-read sudo helper may elevate.'}
UMask=0077
'''


def timer(role):
    return f'''[Unit]
Description=Refresh warehouse {role} observations every ten seconds

[Timer]
OnBootSec=10
OnUnitActiveSec=10
AccuracySec=1
Unit=warehouse-observer-{role}.service

[Install]
WantedBy=timers.target
'''


def reporter_authorization(previous,reporter):
    reporter=check_public(reporter)
    line=f'from="10.89.0.1",restrict,command="/usr/local/lib/warehouse-netops-observer/receive" {reporter} warehouse-observer-reporter'
    matching=[row for row in previous.splitlines() if row.endswith(' warehouse-observer-reporter')]
    if matching and matching!=[line]:raise RuntimeError('Existing restricted reporter authorization differs')
    return previous if matching else previous.rstrip()+'\n'+line+'\n'


def authorize_reporter(reporter,account):
    """Add one restricted key without expanding sshd's existing AllowUsers."""
    login=pwd.getpwnam('warehouse')
    if login.pw_uid!=1000 or login.pw_dir!='/home/warehouse':
        raise RuntimeError('Existing service maintenance login differs')
    ssh=Path(login.pw_dir)/'.ssh'
    if ssh.is_symlink():raise RuntimeError('Unsafe service SSH directory')
    ssh.mkdir(exist_ok=True);ssh.chmod(0o700);os.chown(ssh,login.pw_uid,login.pw_gid)
    wrapper=Path('/usr/local/lib/warehouse-netops-observer/receive')
    write(wrapper,(CODE/'ops/receive-login.py').read_bytes(),0o755)
    auth=ssh/'authorized_keys'
    if auth.is_symlink():raise RuntimeError('Unsafe service authorized_keys path')
    previous=auth.read_text() if auth.exists() else ''
    updated=reporter_authorization(previous,reporter)
    if updated!=previous:
        write(auth,updated,0o600,login.pw_uid,login.pw_gid)
    else:
        info=auth.lstat()
        if info.st_uid!=login.pw_uid or stat.S_IMODE(info.st_mode)!=0o600:
            raise RuntimeError('Existing service authorization ownership or permissions differ')
    # Test only the required privilege drop; no shell or arbitrary sudo command
    # is exposed by the fixed forced-command wrapper.
    run('runuser','-u','warehouse','--','sudo','-n','-u',USER,'--','/usr/bin/true')


def install(value):
    role=value.get('role')
    if role not in ('site','cloud'):raise RuntimeError('Unknown observer installation role')
    account=setup()
    configuration(role,value['expected_peer'],account)
    if role=='cloud':
        public=keypair('reporter_ed25519',account)
        pin('service_known_hosts','192.168.30.10',value['service_host_public_key'],account)
        helper=Path('/usr/local/lib/warehouse-netops-observer/cloud-read')
        write(helper,(CODE/'ops/cloud-read.py').read_bytes(),0o755)
        sudoers=Path('/etc/sudoers.d/warehouse-netops-observer')
        rule=f'{USER} ALL=(root) NOPASSWD: {helper} ""\n'
        if sudoers.exists() and sudoers.read_text()!=rule:raise RuntimeError('Unknown observer sudoers rule exists')
        write(sudoers,rule,0o440)
        run('visudo','-cf',str(sudoers))
    else:
        public=keypair('router_ed25519',account)
        pin('router_known_hosts','192.168.30.1',value['router_host_public_key'],account)
        reporter=check_public(value['reporter_public_key'])
        authorize_reporter(reporter,account)
    name='warehouse-observer-'+role
    write('/etc/systemd/system/'+name+'.service',unit(role))
    write('/etc/systemd/system/'+name+'.timer',timer(role))
    run('systemctl','daemon-reload')
    # Activation is a separate final step after both SSH keys are installed.
    return {'role':role,'public_key':public,'timer':name+'.timer','installed':True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--request',required=True)
    args=parser.parse_args()
    request=Path(args.request)
    if request.is_symlink() or request.stat().st_size>32768:raise RuntimeError('Invalid project installation request')
    print(json.dumps(install(json.loads(request.read_text()))))


if __name__=='__main__':
    main()
