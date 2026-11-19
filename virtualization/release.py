"""Build, publish and roll back immutable warehouse dashboard releases.

Artifacts and backups stay local/private. Network operations use the existing
Guangzhou tunnel only and never use the provisioning/staging network.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid

try:
    from .runtime import PROJECT,RUNTIME,PRIVATE
    from .release_transport import ServiceRelay
except ImportError:
    from runtime import PROJECT,RUNTIME,PRIVATE
    from release_transport import ServiceRelay

ARTIFACTS=RUNTIME/'releases'
REMOTE='/opt/warehouse-netops'
MANAGER=REMOTE+'/private/release-manager.py'
VERSION_RE=re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?')


def command(*args, **kwargs):
    return subprocess.run(args,cwd=PROJECT,check=True,capture_output=True,**kwargs)


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def attempt_path(target):
    """Every transfer owns a new partial; interrupted attempts stay inspectable."""
    target=Path(target)
    return target.with_name(target.name+'.'+uuid.uuid4().hex+'.part')


def publish_private(partial,target):
    """Atomically publish without replacing a concurrently created artifact."""
    partial,target=Path(partial),Path(target)
    if partial.parent.resolve()!=target.parent.resolve() or partial.is_symlink() or not partial.is_file():
        raise RuntimeError('Unexpected private transfer partial')
    partial.chmod(0o600)
    try:
        # A same-directory hard link is an atomic no-replace publication on
        # NTFS and Linux. A plain POSIX rename could overwrite a racing target.
        os.link(partial,target)
    except FileExistsError:
        if target.is_symlink() or not target.is_file() or sha256(target)!=sha256(partial):
            raise RuntimeError('Existing private artifact differs; no file was replaced') from None
    partial.unlink()
    return target


def write_private_manifest(target,value):
    target=Path(target)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or json.loads(target.read_text(encoding='utf-8'))!=value:
            raise RuntimeError('Existing private artifact manifest differs')
        return
    partial=attempt_path(target)
    with partial.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,indent=2)
        stream.flush();os.fsync(stream.fileno())
    publish_private(partial,target)


def download_private(relay,path,target,expected,timeout=120):
    if not re.fullmatch('[0-9a-f]{64}',expected):raise RuntimeError('Invalid private artifact checksum')
    target=Path(target)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or sha256(target)!=expected:
            raise RuntimeError('Existing private artifact checksum differs')
        return target
    partial=attempt_path(target)
    checked(relay,'cat '+shlex.quote(path),output_file=partial,timeout=timeout)
    if sha256(partial)!=expected:raise RuntimeError('Private artifact download checksum differs')
    return publish_private(partial,target)


def validate_image_export(path,image_id):
    """Check gzip integrity and the Docker config content's image identity."""
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id):raise RuntimeError('Invalid image identity')
    with gzip.open(path,'rb') as stream:
        for _ in iter(lambda:stream.read(1024*1024),b''):pass
    with tarfile.open(path,'r:gz') as archive:
        member=archive.getmember('manifest.json')
        if not member.isfile() or member.size>1024*1024:raise RuntimeError('Invalid Docker image export manifest')
        rows=json.load(archive.extractfile(member))
        if not isinstance(rows,list):raise RuntimeError('Invalid Docker image export entries')
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('Config'),str):continue
            config=archive.getmember(row['Config'])
            if not config.isfile() or config.size>4*1024*1024:continue
            payload=archive.extractfile(config).read()
            if hashlib.sha256(payload).hexdigest()==image_id.split(':',1)[1]:return
    raise RuntimeError('Docker image export does not contain the captured image')


def clean_revision():
    if command('git','status','--porcelain','--untracked-files=normal').stdout.strip():
        raise RuntimeError('Commit tracked changes and intended source files before building a release')
    return command('git','rev-parse','HEAD',text=True).stdout.strip()


def source_hash(revision):
    names=command('git','ls-tree','-r','--name-only',revision,'server/app',text=True).stdout.splitlines()
    h=hashlib.sha256()
    for name in names:
        payload=command('git','show',revision+':'+name).stdout
        h.update(name.encode()+b'\0'+hashlib.sha256(payload).digest())
    return h.hexdigest()


def save_image(image,target,image_id=None):
    target=Path(target)
    image_id=image_id or (image if re.fullmatch(r'sha256:[0-9a-f]{64}',image) else command('docker','image','inspect',image,'--format','{{.Id}}',text=True).stdout.strip())
    if target.exists() or target.is_symlink():
        if target.is_symlink():raise RuntimeError('Refusing image-export target symlink')
        validate_image_export(target,image_id)
        return
    partial=attempt_path(target)
    proc=subprocess.Popen(['docker','save',image],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    # docker save normally emits no stderr. Drain it concurrently to avoid
    # blocking if the daemon returns an unusually large diagnostic.
    import threading
    tail=[]
    def drain():
        value=b''
        for part in iter(lambda:proc.stderr.read(65536),b''):value=(value+part)[-4096:]
        tail.append(value)
    thread=threading.Thread(target=drain,daemon=True);thread.start()
    try:
        with partial.open('xb') as output,gzip.GzipFile(filename='',mode='wb',fileobj=output,mtime=0) as packed:
            for chunk in iter(lambda:proc.stdout.read(1024*1024),b''):packed.write(chunk)
        if proc.wait(timeout=60):raise RuntimeError('Docker image export failed')
        thread.join(timeout=2)
        validate_image_export(partial,image_id)
        publish_private(partial,target)
    except BaseException:
        proc.kill();proc.wait()
        raise
    finally:
        proc.stdout.close();proc.stderr.close()


def build(version):
    if not VERSION_RE.fullmatch(version):raise ValueError('Use a semantic release version such as v0.5.0')
    revision=clean_revision();identity=version+'-'+revision[:12]
    output=ARTIFACTS/identity
    if output.exists():raise FileExistsError('This immutable release artifact already exists')
    output.mkdir(parents=True,mode=0o700)
    tree=output/'tree';tree.mkdir()
    archive=command('git','archive',revision,'server').stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for item in tar.getmembers():
            if item.issym() or item.islnk() or Path(item.name).is_absolute() or '..' in Path(item.name).parts:
                raise RuntimeError('Unexpected symbolic or escaping source archive entry')
        tar.extractall(tree,filter='data')
    unit=command('git','show',revision+':virtualization/systemd/warehouse-stack.service').stdout
    (tree/'server/warehouse-stack.service').write_bytes(unit)
    image='warehouse-netops-dashboard:'+identity
    source=source_hash(revision)
    command('docker','build','--platform','linux/amd64','--tag',image,
        '--build-arg','APP_VERSION='+version,'--build-arg','GIT_COMMIT='+revision,
        '--build-arg','SOURCE_SHA256='+source,str(tree/'server'),timeout=600)
    image_id=command('docker','image','inspect',image,'--format','{{.Id}}',text=True).stdout.strip()
    save_image(image,output/'images.tar.gz',image_id=image_id)
    with tarfile.open(output/'source.tar.gz','w:gz') as tar:tar.add(tree/'server',arcname='server')
    manifest={'release_id':identity,'version':version,'git_commit':revision,'source_sha256':source,
        'image_id':image_id,'image_tag':image,'schema_min':1,'schema_max':3,'required_schema':3,
        'created_at':datetime.now(timezone.utc).isoformat(),
        'files':{name:sha256(output/name) for name in ('images.tar.gz','source.tar.gz')}}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    with tarfile.open(output/'bundle.tar','w') as tar:
        for name in ('manifest.json','images.tar.gz','source.tar.gz'):tar.add(output/name,arcname=name)
    manifest['bundle_sha256']=sha256(output/'bundle.tar')
    (output/'build.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest


def checked(relay,cmd,**kwargs):
    code,text=relay.run(cmd,**kwargs)
    if code:raise RuntimeError('Service release operation failed: '+text[-1200:])
    return text


def json_result(text):
    for line in reversed(text.splitlines()):
        try:value=json.loads(line)
        except ValueError:continue
        if isinstance(value,dict):return value
    raise RuntimeError('Service release command did not return a JSON result')


def install_manager(relay):
    data=(PROJECT/'virtualization/release_guest.py').read_bytes()
    cmd='set -eu; umask 077; test -d '+REMOTE+'; test ! -L '+REMOTE+'/private; mkdir -p '+REMOTE+'/private; cat > '+MANAGER+'.part && chmod 600 '+MANAGER+'.part && mv '+MANAGER+'.part '+MANAGER
    checked(relay,cmd,input_data=data,timeout=30)
    result=checked(relay,'sha256sum '+MANAGER)
    if hashlib.sha256(data).hexdigest() not in result:raise RuntimeError('Release manager transfer checksum failed')


def manager(relay,action,*args,timeout=180):
    return json_result(checked(relay,shlex.join(['python3',MANAGER,action,*args]),timeout=timeout))


def read_status(relay):
    # Running the local status implementation in-memory avoids installing a
    # manager or creating state merely to inspect a legacy deployment.
    source=(PROJECT/'virtualization/release_guest.py').read_bytes()
    encoded=base64.b64encode(source).decode('ascii')
    script="import base64,json;scope={'__name__':'warehouse_release_status'};exec(base64.b64decode("+repr(encoded)+"),scope);print(json.dumps(scope['status']()))"
    return json_result(checked(relay,'python3 -c '+shlex.quote(script),timeout=30))


def resolve_artifact(identity):
    root=ARTIFACTS.resolve()
    target=(root/identity).resolve()
    if target.parent!=root or not target.is_dir():raise ValueError('Unknown local immutable release artifact')
    manifest=json.loads((target/'build.json').read_text(encoding='utf-8'))
    if manifest['release_id']!=identity or sha256(target/'bundle.tar')!=manifest['bundle_sha256']:
        raise RuntimeError('Local release artifact checksum or identity changed')
    return target,manifest


def copy_backup(relay,snapshot):
    path=snapshot['path']
    if not re.fullmatch(REMOTE+r'/private/backups/[0-9TZ-]+-[0-9a-f]{8}\.sqlite3',path):
        raise RuntimeError('Unexpected private service backup path')
    directory=PRIVATE/'backups';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=directory/Path(path).name
    download_private(relay,path,target,snapshot['sha256'])
    write_private_manifest(target.with_suffix('.json'),snapshot)
    return str(target)


def backup(relay):
    snapshot=manager(relay,'backup',timeout=150)
    snapshot['local_path']=copy_backup(relay,snapshot)
    private=snapshot.get('private_config')
    if private:
        path=private['path']
        if not re.fullmatch(REMOTE+r'/private/backups/[0-9TZ-]+-[0-9a-f]{8}-config\.tar\.gz',path):
            raise RuntimeError('Unexpected private configuration backup path')
        target=PRIVATE/'backups'/Path(path).name
        download_private(relay,path,target,private['sha256'],timeout=60)
        private['local_path']=str(target)
    return snapshot


def preserve_legacy_image(relay,legacy):
    image=legacy['image_id']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):raise RuntimeError('Invalid captured legacy image identity')
    directory=PRIVATE/'legacy-images';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=directory/(image.split(':')[1]+'.tar.gz')
    manifest=target.with_suffix('.json')
    if target.is_symlink() or manifest.is_symlink():raise RuntimeError('Refusing rollback-image symlink')
    if target.exists():
        validate_image_export(target,image)
        if manifest.is_file():
            saved=json.loads(manifest.read_text())
            if saved.get('image_id')!=image or saved.get('sha256')!=sha256(target):
                raise RuntimeError('Existing rollback image export has no matching checksum')
            return saved
        # Complete data can have been published just before a process stopped.
        # Its config digest proves the image identity before metadata recovery.
        value={'image_id':image,'image_tag':legacy['image_tag'],'sha256':sha256(target),'bytes':target.stat().st_size,'private_path':str(target)}
        write_private_manifest(manifest,value)
        return value
    local=subprocess.run(
        ['docker','image','inspect',image],capture_output=True,cwd=PROJECT)
    if local.returncode==0:
        save_image(image,target,image_id=image)
    else:
        pipeline='sudo -n docker image save '+shlex.quote(image)+' | gzip -c'
        partial=attempt_path(target)
        checked(relay,'bash -o pipefail -c '+shlex.quote(pipeline),output_file=partial,timeout=600)
        validate_image_export(partial,image)
        publish_private(partial,target)
    target.chmod(0o600)
    value={'image_id':image,'image_tag':legacy['image_tag'],'sha256':sha256(target),'bytes':target.stat().st_size,'private_path':str(target)}
    write_private_manifest(manifest,value)
    return value


def restore_check(path):
    path=Path(path).resolve()
    meta_path=path.with_suffix('.json')
    expected=json.loads(meta_path.read_text()) if meta_path.is_file() else None
    if expected is not None and sha256(path)!=expected['sha256']:raise RuntimeError('Backup checksum does not match its manifest')
    # The restore target is a disposable directory. Never write the active DB.
    with tempfile.TemporaryDirectory(prefix='warehouse-restore-') as temp:
        destination=Path(temp)/'restored.sqlite3'
        source=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        target=sqlite3.connect(destination)
        try:source.backup(target)
        finally:source.close();target.close()
        check=sqlite3.connect(destination.as_uri()+'?mode=ro',uri=True)
        try:
            integrity=check.execute('PRAGMA integrity_check').fetchone()[0]
            rows=check.execute('SELECT COUNT(*) FROM readings').fetchone()[0]
            last=check.execute('SELECT seq,received_at FROM readings ORDER BY id DESC LIMIT 1').fetchone()
        finally:check.close()
        passed=integrity=='ok' and (expected is None or (rows==expected['rows'] and list(last or [])==list(expected['last'] or [])))
        result={'passed':passed,'integrity_check':integrity,'rows':rows,'last':last,
            'source_sha256':sha256(path),'isolated_restore':True,'active_database_modified':False}
        if not passed:raise RuntimeError('Isolated database restoration does not match the backup manifest')
        return result


def deploy(relay,identity):
    artifact,manifest=resolve_artifact(identity)
    install_manager(relay)
    legacy=manager(relay,'baseline')
    legacy_export=preserve_legacy_image(relay,legacy)
    local_files={p.relative_to(PROJECT/'server/app').as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (PROJECT/'server/app').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
    capture={'captured_at':datetime.now(timezone.utc).isoformat(),'legacy':legacy,'legacy_export':legacy_export,
        'local_app_files':local_files,'source_match_verified':legacy.get('app_files')==local_files}
    (PRIVATE/'runtime-source-baseline.json').write_text(json.dumps(capture,indent=2),encoding='utf-8')
    # Capture and copy the database BEFORE changing the dashboard.
    snapshot=backup(relay)
    upload_id=uuid.uuid4().hex
    remote=REMOTE+'/incoming/'+identity+'.'+upload_id+'.tar.part'
    cmd='set -eu; umask 077; mkdir -p '+REMOTE+'/incoming; set -C; cat > '+shlex.quote(remote)
    checked(relay,cmd,input_file=artifact/'bundle.tar',timeout=600)
    manager(relay,'receive','--release-id',identity,'--sha256',manifest['bundle_sha256'],'--upload-id',upload_id,timeout=420)
    activated=manager(relay,'activate','--release-id',identity,timeout=240)
    # The management operation itself must also work from Guangzhou's source.
    probe="python3 -c "+shlex.quote("import urllib.request,json;opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));print(json.load(opener.open('http://192.168.30.10:8080/api/version',timeout=5)))")
    # Query through inner SSH first; external route verification is explicit
    # via a cloud-bound HTTP socket, not an ambient proxy environment.
    script="import socket,http.client,json;s=socket.socket();s.settimeout(5);s.bind(('10.89.0.1',0));s.connect(('192.168.30.10',8080));c=http.client.HTTPConnection('192.168.30.10',8080);c.sock=s;c.request('GET','/api/version');r=c.getresponse();print(json.dumps(json.loads(r.read())));c.close()"
    code,text=relay.cloud_run('python3 -c '+shlex.quote(script),timeout=15)
    remote_version=json_result(text) if code==0 else {}
    remote_ok=remote_version.get('version')==manifest['version'] and remote_version.get('git_commit')==manifest['git_commit']
    if not remote_ok:
        rolled=manager(relay,'rollback',timeout=240)
        raise RuntimeError('Guangzhou final version verification failed; application rolled back to '+rolled['release_id'])
    report={'published':True,'release_id':identity,'manifest':manifest,'activation':activated,
        'pre_publish_backup':snapshot,'guangzhou_version_verified':True,'us_accessed':False}
    (PRIVATE/'last-publish.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('build');p.add_argument('--version',required=True)
    p=sub.add_parser('deploy');p.add_argument('--release-id',required=True)
    sub.add_parser('status');sub.add_parser('rollback');sub.add_parser('backup')
    p=sub.add_parser('restore-check');p.add_argument('--backup',required=True,type=Path)
    args=parser.parse_args()
    PRIVATE.mkdir(parents=True,exist_ok=True)
    if args.action=='build':result=build(args.version)
    elif args.action=='restore-check':result=restore_check(args.backup)
    else:
        with ServiceRelay() as relay:
            if args.action=='deploy':result=deploy(relay,args.release_id)
            elif args.action=='status':result=read_status(relay)
            else:
                install_manager(relay)
                result=backup(relay) if args.action=='backup' else manager(relay,args.action,timeout=240)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error_type':type(error).__name__,'error':str(error)}))
        raise SystemExit(1)
