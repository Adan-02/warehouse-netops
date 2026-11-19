"""Download only the exact images pinned by the reviewed project lockfile."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import re
from urllib.parse import urlparse
try:
    from .runtime import IMAGES, PROJECT, download, digest, prepare_dirs
except ImportError:
    from runtime import IMAGES, PROJECT, download, digest, prepare_dirs

OPENWRT = "25.12.6"
OW_BASE = f"https://downloads.openwrt.org/releases/{OPENWRT}/targets/x86/64/"
DEB_BASE = "https://cloud.debian.org/images/cloud/trixie/latest/"


LOCKFILE=PROJECT/'images.lock.json'


def load_lock(path=LOCKFILE):
    value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict) or value.get('schema_version')!=1 or not isinstance(value.get('images'),list) or not value['images']:
        raise ValueError('Invalid image lockfile schema')
    names=set()
    for image in value['images']:
        if not isinstance(image,dict) or set(image)!={'url','filename','checksum','algorithm'}:
            raise ValueError('Invalid image lockfile entry')
        name=image['filename'];algorithm=image['algorithm']
        if not isinstance(name,str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,199}',name) or name in names:
            raise ValueError('Invalid or duplicate locked image filename')
        names.add(name)
        if algorithm not in ('sha256','sha512') or not isinstance(image['checksum'],str) or not re.fullmatch('[0-9a-f]{'+str(64 if algorithm=='sha256' else 128)+'}',image['checksum']):
            raise ValueError('Invalid locked image checksum')
        if not isinstance(image['url'],str):raise ValueError('Invalid locked image URL')
        url=urlparse(image['url'])
        if url.scheme!='https' or url.hostname not in ('downloads.openwrt.org','cloud.debian.org') or url.username or url.password or url.query or url.fragment:
            raise ValueError('Locked images require an official HTTPS download URL')
        if url.path.rsplit('/',1)[-1]!=name:
            raise ValueError('Locked image URL and filename differ')
    return value['images']


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock-file',type=Path,default=LOCKFILE)
    parser.add_argument('--verify-only',action='store_true',help='Only verify existing cached files; no downloads or writes')
    args=parser.parse_args(argv)
    jobs=load_lock(args.lock_file)
    if args.verify_only:
        results=[{'filename':row['filename'],'verified':(IMAGES/row['filename']).is_file() and digest(IMAGES/row['filename'],row['algorithm'])==row['checksum']} for row in jobs]
        print(json.dumps(results,indent=2))
        return 0 if all(row['verified'] for row in results) else 1
    prepare_dirs()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(download,row['url'],IMAGES/row['filename'],row['checksum'],row['algorithm']) for row in jobs]
        for future in futures:
            future.result()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
