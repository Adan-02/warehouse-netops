"""Create sparse Linux guest disks from the locked Debian image.

This is a first-install operation. Existing VMX, seeds, disks, partial outputs
and VMware locks are protected; this is never an upgrade/recovery command.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
try:
    from .runtime import IMAGES,VMS,tool,ensure_stopped,digest
    from .download_images import load_lock
except ImportError:
    from runtime import IMAGES,VMS,tool,ensure_stopped,digest
    from download_images import load_lock

SIZES={'service':16,'iot':4,'office':12}


def verify_base():
    entries=[row for row in load_lock() if row['filename']=='debian-13-generic-amd64.qcow2']
    if len(entries)!=1:raise RuntimeError('Exactly one locked Debian generic AMD64 image is required')
    entry=entries[0];base=IMAGES/entry['filename']
    if not base.is_file() or base.is_symlink() or digest(base,entry['algorithm'])!=entry['checksum']:
        raise RuntimeError('The cached Debian image does not match images.lock.json; verify/download it first')
    return base


def preflight(role):
    if role not in SIZES:raise ValueError('Only service, iot and office disks may be prepared')
    vmx=ensure_stopped(role)
    directory=VMS/role
    if directory.is_symlink():raise RuntimeError('Guest directory symlinks are not accepted')
    protected=(vmx,directory/'seed',directory/'seed.iso',directory/(role+'.vmdk'),
        directory/(role+'.prepare.qcow2'),directory/(role+'.vmdk.partial'),directory/'.prepare-disk.lock')
    protected=(*protected,directory/'.prepare-disk.work')
    if any(path.exists() or path.is_symlink() for path in protected):
        raise FileExistsError(role+': existing VM/configuration/disk or partial preparation is protected')
    return directory


def prepare(role,base):
    directory=preflight(role)
    directory.mkdir(parents=True,exist_ok=True)
    lock=directory/'.prepare-disk.lock';lock.mkdir()
    intermediate=directory/(role+'.prepare.qcow2')
    workspace=directory/'.prepare-disk.work'
    # Keep the final basename even during conversion: a monolithic VMDK's
    # embedded descriptor references that basename, so renaming *.partial
    # directly to *.vmdk would leave an invalid extent reference.
    temporary=workspace/(role+'.vmdk')
    target=directory/(role+'.vmdk')
    prefix='/work/vms/'+role+'/'
    try:
        workspace.mkdir()
        # The official source is never resized or altered.
        tool('qemu-img','convert','-f','qcow2','-O','qcow2','/work/images/'+base.name,prefix+intermediate.name)
        tool('qemu-img','resize',prefix+intermediate.name,str(SIZES[role])+'G')
        tool('qemu-img','convert','-f','qcow2','-O','vmdk','-o','subformat=monolithicSparse',prefix+intermediate.name,prefix+'.prepare-disk.work/'+temporary.name)
        ensure_stopped(role)
        if (directory/(role+'.vmx')).exists():raise RuntimeError('A VM configuration appeared during disk preparation')
        # Hard-link publication fails atomically if a concurrent operation made
        # the final filename. It never replaces an existing active disk.
        os.link(temporary,target)
        temporary.unlink();workspace.rmdir();intermediate.unlink()
        return {'role':role,'capacity_gib':SIZES[role],'disk':str(target),'source_image':base.name,'existing_disk_overwritten':False}
    finally:
        # Only this operation's empty lock directory is removed. On failure the
        # partial files remain visible and are never automatically overwritten.
        lock.rmdir()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role',choices=tuple(SIZES),action='append',help='Default: all three Linux guests')
    args=parser.parse_args(argv)
    roles=args.role or list(SIZES)
    if len(set(roles))!=len(roles):parser.error('Each role may be selected only once')
    # No directory is created until every selected role and source image pass.
    for role in roles:preflight(role)
    base=verify_base()
    import json
    for role in roles:print(json.dumps(prepare(role,base)))


if __name__=='__main__':main()
