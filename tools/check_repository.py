"""Check the Git index for accidentally tracked local state and credentials."""
import argparse
from pathlib import PurePosixPath
import re
import subprocess


def problems(name, data):
    path=PurePosixPath(name)
    if set(path.parts)&{'.secrets','.cache','.local','archive','backups','releases','__pycache__','.pio'}:
        return ['local-state path']
    if path.name=='.env' or (path.suffix=='.env' and not name.endswith('.env.example')):
        return ['environment credentials']
    if path.suffix.lower() in {'.key','.pem','.sqlite3','.db','.vmdk','.qcow2','.iso','.part'}:
        return ['private data or runtime artifact']
    text=data.decode('utf-8',errors='replace')
    signatures=(
        r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----[\s\S]{30,}?-----END',
        r'\bgh[pousr]_[A-Za-z0-9]{30,}\b',
        r'\bgithub_pat_[A-Za-z0-9_]{30,}\b',
    )
    return ['credential signature '+str(i+1) for i,p in enumerate(signatures) if re.search(p,text)]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--staged',action='store_true')
    args=parser.parse_args()
    names=subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
    failures=[]
    for name in filter(None,names):
        data=subprocess.check_output(['git','show',':'+name])
        failures.extend((name,reason) for reason in problems(name,data))
    for name,reason in failures: print(name+': '+reason)
    print('Repository index:',sum(bool(n) for n in names),'files,',len(failures),'findings')
    return bool(failures)


if __name__=='__main__':
    raise SystemExit(main())
