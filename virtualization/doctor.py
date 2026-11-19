"""Read-only local prerequisites and image/VM integrity inspection.

No network settings, credentials, guests or cloud services are changed. This
reports installation readiness, not a fresh business access-matrix acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

try:
    from .lab_profile import load_profile,PROJECT
    from .download_images import load_lock,LOCKFILE
except ImportError:
    from lab_profile import load_profile,PROJECT
    from download_images import load_lock,LOCKFILE

ROLES=('router','service','iot','office')


def run(arguments,timeout=12):
    try:
        value=subprocess.run(arguments,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,cwd=PROJECT)
        return {'exit_code':value.returncode,'output':value.stdout.strip()[-2000:]}
    except (OSError,subprocess.TimeoutExpired) as error:
        return {'exit_code':None,'error_type':type(error).__name__}


def checksum(path,algorithm):
    h=hashlib.new(algorithm)
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def vm_config(path,role,running):
    if not path.is_file() or path.is_symlink():return {'status':'missing','vmx_present':False}
    fields=dict(re.findall(r'^\s*([^#\s][^=]*?)\s*=\s*"([^"\r\n]*)"\s*$',path.read_text(encoding='utf-8',errors='replace'),re.M))
    physical=[key for key,value in fields.items() if re.fullmatch(r'ethernet[0-9]+\.present',key) and value.upper()=='TRUE']
    count=4 if role=='router' else 1
    disk=fields.get('ide0:0.fileName')
    disk_path=Path(disk) if disk else None
    if disk_path is not None and not disk_path.is_absolute():disk_path=path.parent/disk_path
    locks=[entry.name for entry in path.parent.glob('*.lck')]
    serial=fields.get('serial0.present','').upper()=='TRUE' and fields.get('serial0.fileType')=='pipe'
    okay=len(physical)==count and disk_path is not None and disk_path.is_file() and serial
    # Locks on running VMs are expected. Never delete or acquire their locks.
    stale=bool(locks) and not running
    return {'status':'pass' if okay and not stale else 'warning' if okay else 'failed',
        'vmx_present':True,'physical_nics':len(physical),'expected_nics':count,
        'active_disk_present':disk_path is not None and disk_path.is_file(),
        'serial_pipe_configured':serial,'running':running,'lock_entries':locks,
        'action':'Close the powered-off VM normally before editing; do not delete locks' if stale else None}


def inspect(profile,*,verify_images=True):
    rows=[]
    def add(name,status,evidence):rows.append({'name':name,'status':status,'evidence':evidence})
    add('python','pass' if sys.version_info>=(3,11) else 'failed',{'version':sys.version.split()[0]})
    git=run(['git','--version'])
    add('git','pass' if git['exit_code']==0 else 'failed',git)
    repo=run(['git','rev-parse','--is-inside-work-tree'])
    add('source_repository','pass' if repo.get('output')=='true' else 'failed',repo)
    docker=run(['docker','version','--format','{{.Server.Version}}'])
    add('docker_engine','pass' if docker['exit_code']==0 and docker.get('output') else 'failed',docker)
    compose=run(['docker','compose','version','--short'])
    add('docker_compose','pass' if compose['exit_code']==0 else 'failed',compose)
    vmrun=Path(profile['vmrun'])
    listing=run([str(vmrun),'list']) if vmrun.is_file() else {'exit_code':None,'error_type':'FileNotFoundError'}
    add('vmware_vmrun','pass' if listing['exit_code']==0 else 'failed',{'available':vmrun.is_file(),'exit_code':listing['exit_code'],'error_type':listing.get('error_type')})
    active={line.strip().casefold() for line in listing.get('output','').splitlines()[1:]}
    images=Path(profile['runtime_dir'])/'images'
    for item in load_lock(LOCKFILE):
        path=images/item['filename'];exists=path.is_file() and not path.is_symlink()
        verified=checksum(path,item['algorithm'])==item['checksum'] if verify_images and exists else None
        add('image_'+item['filename'],'missing' if not exists else 'pass' if verified is True else 'failed' if verified is False else 'not_checked',
            {'present':exists,'algorithm':item['algorithm'],'locked_checksum_match':verified})
    for role in ROLES:
        path=Path(profile['runtime_dir'])/'vms'/role/(role+'.vmx')
        value=vm_config(path,role,str(path).casefold() in active)
        add('vm_'+role,value['status'],value)
    prefs=Path(profile['vmware_preferences'])
    if prefs.is_file() and not prefs.is_symlink():
        text=prefs.read_text(encoding='utf-8',errors='replace')
        names=re.findall(r'pref\.namedPVNs\d+\.name\s*=\s*"([^"]+)"',text)
        present={name:name in names for name in ('wh-office','wh-iot','wh-service')}
        add('lan_segments','pass' if all(present.values()) else 'failed',present)
    else:add('lan_segments','missing',{'preferences_present':False})
    passed=all(row['status']=='pass' for row in rows)
    return {'observed_at':datetime.now(timezone.utc).isoformat(),'scope':'Read-only local installation and locked artifact checks',
        'passed':passed,'checks':rows,'network_changed':False,'guests_started_or_stopped':False,
        'credentials_read':False,'cloud_accessed':False,'business_health_tested':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--quick',action='store_true',help='Skip image hashing; those checks remain not_checked')
    args=parser.parse_args(argv)
    profile=load_profile(args.profile)
    report=inspect(profile,verify_images=not args.quick)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 2


if __name__=='__main__':raise SystemExit(main())
