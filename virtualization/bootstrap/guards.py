"""Fail closed before any staging write, console or SSH connection."""
import argparse
import re

from virtualization.runtime import load_paths


def parse_bootstrap(description, argv=None):
    return argparse.ArgumentParser(description=description).parse_args(argv)


def require_staging(paths=None):
    paths = load_paths() if paths is None else paths
    receipt = paths.private/'last-publish.json'
    if receipt.exists() or receipt.is_symlink():
        raise RuntimeError('This lab has a release deployment. Use release.py for updates; bootstrap is first-install only.')
    for role in ('service', 'iot', 'office'):
        vmx = paths.vms/role/(role+'.vmx')
        if not vmx.exists():
            continue
        if vmx.is_symlink():
            raise RuntimeError('Staging VM configuration must not be a symlink')
        fields = dict(re.findall(r'^\s*([^#\s][^=]*?)\s*=\s*"([^"\r\n]*)"\s*$', vmx.read_text(encoding='utf-8'), re.M))
        if fields.get('ethernet0.connectionType') != 'custom' or fields.get('ethernet0.vnet') != 'VMnet8':
            raise RuntimeError(role+': not a staging VMnet8 guest. Bootstrap cannot modify an isolated lab.')
