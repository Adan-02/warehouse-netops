"""Owned service-VM release transactions. Run as the lab warehouse user.

This file is uploaded through the pinned dedicated tunnel. It never changes
networking, broker credentials, other Docker projects or existing data volumes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:  # Pure validation helpers are also tested on Windows.
    fcntl=None
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT=Path('/opt/warehouse-netops')
VOLUME='warehouse-netops_dashboard_data'
CONTAINER='warehouse-netops-dashboard-1'
RELEASE_RE=re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?-[0-9a-f]{12}')


def run(*args, timeout=120, check=True, input=None):
    result=subprocess.run(args,input=input,capture_output=True,text=not isinstance(input,bytes),timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Project command failed: '+str(args[0])+' '+str(result.returncode))
    return result


def docker(*args, **kwargs):
    return run('sudo','-n','docker',*args,**kwargs)


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        for part in iter(lambda:source.read(1024*1024),b''):value.update(part)
    return value.hexdigest()


def atomic_json(path,value):
    temp=path.with_name(path.name+'.tmp')
    with temp.open('w',encoding='utf-8') as stream:json.dump(value,stream,indent=2)
    temp.chmod(0o600);os.replace(temp,path)


def guard_root():
    if not ROOT.is_dir() or ROOT.is_symlink() or not (ROOT/'server/.env').is_file():
        raise RuntimeError('Existing owned service project and credentials are required')
    if os.getuid()!=1000 or run('hostname').stdout.strip()!='warehouse-service':
        raise RuntimeError('Run only as warehouse on the owned service VM')
    for name in ('releases','shared','private','incoming'):
        path=ROOT/name
        if path.is_symlink():raise RuntimeError('Unexpected project-directory symlink')
        path.mkdir(mode=0o700,exist_ok=True)


@contextmanager
def transaction():
    guard_root()
    if fcntl is None:raise RuntimeError('Release transactions require Linux file locking')
    with (ROOT/'private/release.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def env_values(path):
    return dict(line.split('=',1) for line in path.read_text().splitlines() if line and not line.startswith('#') and '=' in line)


def write_env(path,values):
    temp=path.with_suffix('.tmp')
    temp.write_text(''.join(key+'='+value+'\n' for key,value in values.items()),encoding='utf-8')
    temp.chmod(0o600);os.replace(temp,path)


def current_path():
    link=ROOT/'current'
    if not link.is_symlink():return None
    path=link.resolve()
    if path.parent != ROOT/'releases':raise RuntimeError('Current release escaped project releases')
    return path


def point(name,target):
    if target.parent!=ROOT/'releases':raise RuntimeError('Invalid project release pointer')
    link=ROOT/name;temp=ROOT/(name+'.next')
    if temp.exists() or temp.is_symlink():temp.unlink()
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp,link)


def compose(path,*args):
    return docker('compose','--env-file',str(ROOT/'shared/server.env'),
        '--project-name','warehouse-netops','-f',str(path/'server/compose.yaml'),*args,timeout=180)


def http(path):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://192.168.30.10:8080'+path,timeout=4) as response:return json.load(response)


def schema_version():
    script="""import sqlite3,json
db=sqlite3.connect('file:/data/warehouse.sqlite3?mode=ro',uri=True)
tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
version=1
if 'schema_meta' in tables:
 columns=[r[1] for r in db.execute('PRAGMA table_info(schema_meta)')]
 if {'name','value'}<=set(columns):
  row=db.execute("SELECT value FROM schema_meta WHERE name='schema_version'").fetchone()
  if row:version=int(row[0])
 for name in ('version','schema_version'):
  if name in columns:
   row=db.execute('SELECT '+name+' FROM schema_meta ORDER BY rowid DESC LIMIT 1').fetchone()
   if row:version=int(row[0])
   break
print(json.dumps({'schema_version':version,'columns':[r[1] for r in db.execute('PRAGMA table_info(readings)')]}))
"""
    return json.loads(docker('exec',CONTAINER,'python','-c',script).stdout)


def existing_baseline(legacy):
    """Recover only a complete published legacy capture, never a partial tree."""
    manifest=legacy/'manifest.json'
    if legacy.is_symlink() or not legacy.is_dir() or manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError('Existing legacy release is incomplete; refusing to replace it')
    value=json.loads(manifest.read_text())
    image=value.get('image_id','')
    tag='warehouse-netops-dashboard:legacy-'+image.removeprefix('sha256:')[:12]
    if (value.get('release_id')!='legacy' or value.get('legacy') is not True
            or not re.fullmatch(r'sha256:[0-9a-f]{64}',image) or value.get('image_tag')!=tag):
        raise RuntimeError('Existing legacy manifest identity differs')
    compose_file=legacy/'server/compose.yaml'
    if compose_file.is_symlink() or not compose_file.is_file() or ('    image: '+tag+'\n') not in compose_file.read_text():
        raise RuntimeError('Existing legacy Compose does not pin its captured image')
    expected=value.get('release_files')
    if not isinstance(expected,dict) or not expected:
        raise RuntimeError('Existing legacy release lacks its file checksum manifest')
    actual={p.relative_to(legacy).as_posix():digest(p) for p in (legacy/'server').rglob('*') if p.is_file()}
    if any(p.is_symlink() for p in (legacy/'server').rglob('*')) or actual!=expected:
        raise RuntimeError('Existing legacy release file checksums differ')
    return value


def baseline():
    saved=ROOT/'private/legacy-baseline.json'
    legacy=ROOT/'releases/legacy'
    if saved.is_symlink():raise RuntimeError('Unexpected legacy capture marker symlink')
    if saved.exists():
        value=existing_baseline(legacy)
        if json.loads(saved.read_text())!=value:raise RuntimeError('Legacy capture markers differ')
        return value
    if legacy.exists() or legacy.is_symlink():
        # A process may have completed the atomic directory publication but
        # stopped before recording the second marker. Validate then complete it.
        value=existing_baseline(legacy)
        atomic_json(saved,value)
        return value
    original=(ROOT/'server/compose.yaml').read_text(encoding='utf-8')
    if '  dashboard:\n    build: .' not in original:
        raise RuntimeError('Unrecognized legacy Compose dashboard definition; refusing an implicit rollback image')
    item=json.loads(docker('inspect',CONTAINER).stdout)[0]
    image_id=item['Image']
    mounts={m['Name']:m.get('Destination') for m in item['Mounts'] if m['Type']=='volume'}
    if mounts.get(VOLUME)!='/data':raise RuntimeError('Dashboard uses an unexpected data volume')
    source_script="""import pathlib,hashlib,json
root=pathlib.Path('/app/app');rows={}
for p in sorted(root.rglob('*')):
 if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc':rows[p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps(rows,sort_keys=True))
"""
    source=json.loads(docker('exec',CONTAINER,'python','-c',source_script).stdout)
    tag='warehouse-netops-dashboard:legacy-'+image_id.split(':')[1][:12]
    value={'release_id':'legacy','image_id':image_id,'image_tag':tag,'schema_min':1,'schema_max':3,
        'legacy':True,'app_files':source,'captured_at':datetime.now(timezone.utc).isoformat()}
    staging=Path(tempfile.mkdtemp(prefix='.legacy-capture-',dir=ROOT/'releases'))
    try:
        shutil.copytree(ROOT/'server',staging/'server',ignore=shutil.ignore_patterns('.env','runtime','__pycache__','tests'))
        compose_file=staging/'server/compose.yaml'
        # Use the preflighted text, not a second unverified read after copying.
        compose_file.write_text(original.replace('  dashboard:\n    build: .','  dashboard:\n    image: '+tag+'\n    build: .',1),encoding='utf-8')
        value['release_files']={p.relative_to(staging).as_posix():digest(p) for p in (staging/'server').rglob('*') if p.is_file()}
        atomic_json(staging/'manifest.json',value)
        docker('tag',image_id,tag)
        if legacy.exists() or legacy.is_symlink():raise RuntimeError('Legacy release appeared during capture')
        staging.rename(legacy)
    finally:
        # This exact path was created above. Never remove a pre-existing legacy
        # directory, and refuse cleanup if its identity somehow changed.
        if staging.exists():
            if staging.parent.resolve()!=(ROOT/'releases').resolve() or staging.is_symlink() or not staging.name.startswith('.legacy-capture-'):
                raise RuntimeError('Refusing cleanup of an unexpected legacy staging path')
            shutil.rmtree(staging)
    atomic_json(saved,value)
    return value


def bootstrap_layout():
    old=baseline()
    shared=ROOT/'shared/server.env'
    if not shared.exists():
        values=env_values(ROOT/'server/.env')
        if values.get('SERVICE_BIND_IP')!='192.168.30.10':raise RuntimeError('Final service bind address is required')
        if not values.get('MQTT_DEVICE_PASSWORD') or not values.get('MQTT_VIEWER_PASSWORD'):raise RuntimeError('Existing credentials are incomplete')
        values['DASHBOARD_IMAGE']=old['image_tag']
        values['WAREHOUSE_CONFIG_HOST']=str(ROOT/'shared/config.json')
        values['OBSERVATIONS_HOST_DIR']='/var/lib/warehouse-netops-observer/observations'
        write_env(shared,values)
    if current_path() is None:point('current',ROOT/'releases/legacy')
    return old


def backup():
    folder=ROOT/'private/backups';folder.mkdir(mode=0o700,exist_ok=True)
    identifier=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+os.urandom(4).hex()
    destination=folder/(identifier+'.sqlite3')
    source=docker('volume','inspect',VOLUME,'--format','{{.Mountpoint}}').stdout.strip()
    if not source.startswith('/var/lib/docker/volumes/'+VOLUME+'/'):
        raise RuntimeError('Unexpected Docker data volume location')
    script="""import sqlite3,pathlib,os,json,hashlib,sys
source,target=sys.argv[1:]
path=pathlib.Path(target)
if path.exists() or path.is_symlink():raise RuntimeError('Backup target exists')
db=sqlite3.connect('file:'+source+'?mode=ro',uri=True,timeout=10)
out=sqlite3.connect(target)
try:db.backup(out,pages=256,sleep=.05)
finally:out.close();db.close()
os.chown(target,1000,1000);os.chmod(target,0o600)
db=sqlite3.connect('file:'+target+'?mode=ro',uri=True)
check=db.execute('PRAGMA integrity_check').fetchone()[0]
rows=db.execute('SELECT COUNT(*) FROM readings').fetchone()[0]
last=db.execute('SELECT seq,received_at FROM readings ORDER BY id DESC LIMIT 1').fetchone()
print(json.dumps({'path':target,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'rows':rows,'last':last,'integrity_check':check}))
"""
    result=run('sudo','-n','python3','-c',script,source+'/warehouse.sqlite3',str(destination),timeout=120)
    value=json.loads(result.stdout)
    if value['integrity_check']!='ok':raise RuntimeError('Database snapshot integrity verification failed')
    value['created_at']=datetime.now(timezone.utc).isoformat()
    config_path=folder/(identifier+'-config.tar.gz')
    with tarfile.open(config_path,'w:gz') as package:
        credentials=ROOT/'shared/server.env'
        if not credentials.is_file():credentials=ROOT/'server/.env'
        if credentials.is_symlink():raise RuntimeError('Credentials backup refuses a symlink')
        package.add(credentials,arcname='server.env',recursive=False)
        settings=ROOT/'shared/config.json'
        if settings.is_file() and not settings.is_symlink():package.add(settings,arcname='config.json',recursive=False)
    config_path.chmod(0o600)
    value['private_config']={'path':str(config_path),'sha256':digest(config_path),'contains_credentials':True}
    atomic_json(destination.with_suffix('.json'),value)
    return value


def extract_checked(archive,destination):
    with tarfile.open(archive,'r:*') as tar:
        members=tar.getmembers()
        for member in members:
            path=Path(member.name)
            if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise RuntimeError('Unsafe member in project release archive')
            if path.parts and path.parts[0] not in ('server','manifest.json','images.tar.gz','source.tar.gz'):
                raise RuntimeError('Unexpected release archive path')
        tar.extractall(destination,members=members,filter='data' if sys.version_info>=(3,12) else None)


def receive(release_id,sha256,upload_id=None):
    if not RELEASE_RE.fullmatch(release_id) or not re.fullmatch('[0-9a-f]{64}',sha256):raise ValueError('Invalid release identity/checksum')
    if upload_id is not None and not re.fullmatch('[0-9a-f]{32}',upload_id):raise ValueError('Invalid upload attempt identity')
    archive=ROOT/'incoming'/(release_id+('.'+upload_id if upload_id else '')+'.tar.part')
    if not archive.is_file() or archive.is_symlink() or digest(archive)!=sha256:raise RuntimeError('Uploaded project bundle checksum differs')
    target=ROOT/'releases'/release_id
    if target.exists():raise RuntimeError('This release directory already exists; query status before retrying')
    if shutil.disk_usage(ROOT).free < max(1024*1024*1024,archive.stat().st_size*8):
        raise RuntimeError('Insufficient free space for image, rollback and database snapshot')
    target.mkdir(mode=0o700)
    try:
        extract_checked(archive,target)
        manifest=json.loads((target/'manifest.json').read_text())
        if manifest['release_id']!=release_id:raise RuntimeError('Release manifest identity differs')
        for name in ('images.tar.gz','source.tar.gz'):
            if digest(target/name)!=manifest['files'][name]:raise RuntimeError('Inner release artifact checksum differs')
        extract_checked(target/'source.tar.gz',target)
        if not (target/'server/warehouse-stack.service').is_file() or not (target/'server/config.example.json').is_file():
            raise RuntimeError('Prepared release lacks its startup unit or configuration template')
        image=manifest['image_tag']
        if not re.fullmatch('warehouse-netops-dashboard:'+re.escape(release_id),image):raise RuntimeError('Unexpected release image tag')
        docker('load','-i',str(target/'images.tar.gz'),timeout=300)
        actual=docker('image','inspect',image,'--format','{{.Id}}').stdout.strip()
        if actual!=manifest['image_id']:raise RuntimeError('Loaded image ID differs from manifest')
        preflight=docker('run','--rm','--network','none','--entrypoint','python',image,'-c',
            'import app.main,os,json;print(json.dumps({k:os.environ.get(k) for k in ("APP_VERSION","GIT_COMMIT","SOURCE_SHA256")}))',timeout=45)
        meta=json.loads(preflight.stdout)
        if meta!={'APP_VERSION':manifest['version'],'GIT_COMMIT':manifest['git_commit'],'SOURCE_SHA256':manifest['source_sha256']}:
            raise RuntimeError('Image version metadata differs from manifest')
        atomic_json(target/'prepared.json',{'prepared':True,'at':datetime.now(timezone.utc).isoformat()})
        archive.rename(archive.with_suffix(''))
        return {'prepared':True,'release_id':release_id,'image_id':actual}
    except BaseException:
        atomic_json(target/'failed.json',{'prepared':False,'at':datetime.now(timezone.utc).isoformat()})
        raise


def wait_ready(manifest,timeout=120):
    deadline=time.monotonic()+timeout
    first=None;last_error=None
    while time.monotonic()<deadline:
        try:
            item=http('/api/status')
            if not item.get('broker_connected') or item.get('data_status')!='online' or item.get('device_id')!='cabinet-01':
                raise RuntimeError('Business telemetry is not ready')
            image=docker('inspect',CONTAINER,'--format','{{.Image}}').stdout.strip()
            if image!=manifest['image_id']:raise RuntimeError('Running image does not match release')
            if not manifest.get('legacy'):
                if item.get('storage_state')!='healthy':
                    raise RuntimeError('Telemetry storage is not healthy')
                version=http('/api/version')
                if version.get('version')!=manifest['version'] or version.get('git_commit')!=manifest['git_commit'] or version.get('source_sha256')!=manifest['source_sha256']:
                    raise RuntimeError('Running API version does not match release')
            if first is not None and item.get('last_seen_at')!=first.get('last_seen_at'):
                return {'status':item,'image_id':image,'sampling_progressed':True}
            first=item
        except Exception as error:last_error=type(error).__name__
        time.sleep(2)
    raise RuntimeError('Release business readiness timed out ('+str(last_error)+')')


def activate(release_id):
    target=ROOT/'releases'/release_id
    if not RELEASE_RE.fullmatch(release_id) or not (target/'prepared.json').is_file():raise RuntimeError('Prepared release required')
    bootstrap_layout()
    run('sudo','-n','install','-d','-m','0755','/var/lib/warehouse-netops-observer/observations')
    previous=current_path()
    manifest=json.loads((target/'manifest.json').read_text())
    live_schema=schema_version()['schema_version']
    if not manifest['schema_min']<=live_schema<=manifest['schema_max']:raise RuntimeError('Current database schema is incompatible with the target release')
    snapshot=backup()
    values=env_values(ROOT/'shared/server.env')
    old_values=dict(values)
    if not (ROOT/'shared/config.json').exists():shutil.copy2(target/'server/config.example.json',ROOT/'shared/config.json')
    values['DASHBOARD_IMAGE']=manifest['image_tag']
    try:
        write_env(ROOT/'shared/server.env',values)
        compose(target,'up','-d','--no-deps','--no-build','--pull','never','dashboard')
        checked=wait_ready(manifest)
        point('previous',previous);point('current',target)
        install_unit(target)
        record={'activated':True,'release_id':release_id,'previous':previous.name,'backup':snapshot,'verification':checked,'at':datetime.now(timezone.utc).isoformat()}
        atomic_json(ROOT/'private/last-release.json',record)
        return record
    except BaseException:
        write_env(ROOT/'shared/server.env',old_values)
        compose(previous,'up','-d','--no-deps','--no-build','--pull','never','dashboard')
        old_manifest=json.loads((previous/'manifest.json').read_text())
        verified=wait_ready(old_manifest)
        point('current',previous)
        atomic_json(ROOT/'private/last-failed-release.json',{'release_id':release_id,'rollback_verified':True,'verification':verified,'at':datetime.now(timezone.utc).isoformat()})
        raise


def install_unit(target):
    unit=target/'server/warehouse-stack.service'
    if not unit.is_file():raise RuntimeError('Release systemd unit is missing')
    run('sudo','-n','install','-m','0644',str(unit),'/etc/systemd/system/warehouse-stack.service')
    run('sudo','-n','systemctl','daemon-reload')
    run('sudo','-n','systemctl','enable','warehouse-stack.service')


def rollback():
    bootstrap_layout()
    link=ROOT/'previous'
    if not link.is_symlink():raise RuntimeError('No previous release is recorded')
    target=link.resolve()
    if target.parent!=ROOT/'releases':raise RuntimeError('Previous release escaped project root')
    previous=current_path();manifest=json.loads((target/'manifest.json').read_text())
    version=schema_version()['schema_version']
    if not manifest['schema_min']<=version<=manifest['schema_max']:raise RuntimeError('Rollback target is incompatible with the live database')
    snapshot=backup();values=env_values(ROOT/'shared/server.env');original=dict(values)
    values['DASHBOARD_IMAGE']=manifest['image_tag']
    try:
        write_env(ROOT/'shared/server.env',values)
        compose(target,'up','-d','--no-deps','--no-build','--pull','never','dashboard')
        checked=wait_ready(manifest)
        point('previous',previous);point('current',target)
    except BaseException:
        write_env(ROOT/'shared/server.env',original)
        compose(previous,'up','-d','--no-deps','--no-build','--pull','never','dashboard')
        wait_ready(json.loads((previous/'manifest.json').read_text()))
        point('current',previous)
        raise
    record={'rolled_back':True,'release_id':target.name,'database_restored':False,'backup':snapshot,'verification':checked,'at':datetime.now(timezone.utc).isoformat()}
    atomic_json(ROOT/'private/last-rollback.json',record)
    return record


def status():
    path=current_path()
    value={'current':path.name if path else 'legacy-unmanaged','previous':None}
    if (ROOT/'previous').is_symlink():value['previous']=(ROOT/'previous').resolve().name
    value['image_id']=docker('inspect',CONTAINER,'--format','{{.Image}}').stdout.strip()
    value['release_state_present']=(ROOT/'private/last-release.json').is_file()
    if path is not None:
        value['manifest']=json.loads((path/'manifest.json').read_text())
    else:
        script="""from pathlib import Path
import hashlib,json
root=Path('/app/app');h=hashlib.sha256()
for p in sorted(root.rglob('*')):
 if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc':h.update(p.relative_to(root).as_posix().encode()+b'\\0'+hashlib.sha256(p.read_bytes()).digest())
print(json.dumps({'app_tree_sha256':h.hexdigest()}))
"""
        value['legacy_source']=json.loads(docker('exec',CONTAINER,'python','-c',script).stdout)
    try:value['business']=http('/api/status')
    except Exception:value['business']={'state':'unreachable'}
    try:value['version']=http('/api/version')
    except Exception:value['version']=None
    return value


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('baseline','receive','activate','rollback','status','backup'))
    parser.add_argument('--release-id');parser.add_argument('--sha256');parser.add_argument('--upload-id')
    args=parser.parse_args()
    if args.action=='status':
        if not ROOT.is_dir() or ROOT.is_symlink():raise RuntimeError('Owned project directory unavailable')
        result=status()
    else:
        with transaction():
            if args.action=='baseline':result=baseline()
            elif args.action=='receive':result=receive(args.release_id,args.sha256,args.upload_id)
            elif args.action=='activate':result=activate(args.release_id)
            elif args.action=='rollback':result=rollback()
            else:result=backup()
    print(json.dumps(result,separators=(',',':')))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error_type':type(error).__name__,'error':str(error)}))
        raise SystemExit(1)
