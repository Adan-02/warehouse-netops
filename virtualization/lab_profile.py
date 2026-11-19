"""Load machine paths without putting personal configuration into Git."""
from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil

PROJECT=Path(__file__).resolve().parents[1]
KNOWN={'schema_version','runtime_dir','vmrun','vmware_preferences','cloud_ssh_alias','tools_image'}


def defaults():
    candidates=[shutil.which('vmrun'), 'D:/VMware/vmrun.exe',
        str(Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'VMware/VMware Workstation/vmrun.exe')]
    vmrun=next((value for value in candidates if value and Path(value).is_file()),'D:/VMware/vmrun.exe')
    roaming=Path(os.environ.get('APPDATA',str(Path.home()/'AppData/Roaming')))
    return {'schema_version':1,'runtime_dir':str(Path.home()/'Documents/Codex/warehouse-netops-runtime'),
        'vmrun':vmrun,'vmware_preferences':str(roaming/'VMware/preferences.ini'),
        'cloud_ssh_alias':'tencent-qq-ops','tools_image':'warehouse-vm-tools:20261119'}


def load_profile(path=None):
    source=Path(path or os.environ.get('WAREHOUSE_LAB_PROFILE') or PROJECT/'.local/lab.json')
    value=defaults()
    if source.is_symlink():raise ValueError('Lab profile must not be a symlink')
    if source.exists():
        if not source.is_file() or source.is_symlink():raise ValueError('Lab profile must be a regular file')
        overrides=json.loads(source.read_text(encoding='utf-8-sig'))
        if not isinstance(overrides,dict) or set(overrides)-KNOWN:raise ValueError('Unknown lab profile fields')
        if type(overrides.get('schema_version',1)) is not int or overrides.get('schema_version',1)!=1:raise ValueError('Unsupported lab profile schema')
        value.update(overrides)
    elif path is not None or os.environ.get('WAREHOUSE_LAB_PROFILE'):
        raise FileNotFoundError('The explicitly selected lab profile is missing')
    for key in ('runtime_dir','vmrun','vmware_preferences'):
        item=value[key]
        if not isinstance(item,str) or not item or '\x00' in item or '\n' in item or '\r' in item:
            raise ValueError('Invalid lab profile path: '+key)
        item=os.path.expandvars(os.path.expanduser(item))
        if not Path(item).is_absolute() and not PureWindowsPath(item).is_absolute():
            raise ValueError('Use absolute machine paths in the lab profile: '+key)
        value[key]=item
    if not isinstance(value['cloud_ssh_alias'],str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',value['cloud_ssh_alias']):
        raise ValueError('Invalid cloud SSH alias')
    if not isinstance(value['tools_image'],str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_./:-]{0,159}',value['tools_image']):
        raise ValueError('Invalid local tools image reference')
    return value
