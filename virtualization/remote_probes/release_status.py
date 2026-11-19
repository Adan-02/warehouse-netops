"""One-shot read-only release inventory executed on the service VM.

This program can inspect pointers, image identity and HTTP state only. It has
no installation, activation, rollback or filesystem-creation path.
"""
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT=Path('/opt/warehouse-netops')
CONTAINER='warehouse-netops-dashboard-1'

def run(*args, timeout=120, check=True, input=None):
    result=subprocess.run(args,input=input,capture_output=True,text=not isinstance(input,bytes),timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Project command failed: '+str(args[0])+' '+str(result.returncode))
    return result

def docker(*args, **kwargs):
    return run('sudo','-n','docker',*args,**kwargs)

def current_path():
    link=ROOT/'current'
    if not link.is_symlink():return None
    path=link.resolve()
    if path.parent != ROOT/'releases':raise RuntimeError('Current release escaped project releases')
    return path

def http(path):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://192.168.30.10:8080'+path,timeout=4) as response:return json.load(response)

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

if __name__=='__main__':
    if not ROOT.is_dir() or ROOT.is_symlink():raise RuntimeError('Owned project directory unavailable')
    print(json.dumps(status(),separators=(',',':')))
