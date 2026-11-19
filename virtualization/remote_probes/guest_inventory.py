"""Runs on a Debian VM; collect physical NIC addresses and IPv4 routes."""
import json
import pathlib
import subprocess


def inventory(parameters, result):
    links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show'], text=True))
    nics = []
    for item in links:
        name = item['ifname']
        if pathlib.Path('/sys/class/net/' + name + '/device').exists():
            nics.append({'name': name, 'addresses': [a['local'] + '/' + str(a['prefixlen'])
                for a in item.get('addr_info', []) if a.get('family') == 'inet'],
                'up': 'UP' in item.get('flags', [])})
    result['nics'] = nics
    result['routes'] = json.loads(subprocess.check_output(
        ['ip', '-j', '-4', 'route', 'show'], text=True))
