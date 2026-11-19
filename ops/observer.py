"""One-shot observers for systemd timers; no loops or overlapping probe jobs."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

from .common import MAX_SNAPSHOT_BYTES, atomic_json, component, iso_time, private_config, validate_snapshot
from .webhook import DeliveryStore

ROUTER = '192.168.30.1'
SERVICE = '192.168.30.10'
CLOUD_SOURCE = '10.89.0.1'
OBSERVATIONS = '/var/lib/warehouse-netops-observer/observations'
TIMEOUT = 3


def run_command(arguments, *, data=None, timeout=TIMEOUT):
    """No shell, no credential-bearing command line, bounded trusted output."""
    result = subprocess.run(arguments, input=data, capture_output=True, timeout=timeout)
    if len(result.stdout) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Fixed observer command returned oversized output')
    if result.returncode:
        raise RuntimeError('Fixed observer command failed')
    return result.stdout.decode('utf-8', errors='strict')


def ssh_arguments(config, *, report=False):
    directory = str(config.get('state_dir', '/var/lib/warehouse-netops-observer/private'))
    home = PurePosixPath(directory) if directory.startswith('/') else Path(directory)
    if not home.is_absolute() or '\n' in str(home) or '\r' in str(home):
        raise ValueError('Observer state directory must be an absolute local path')
    key = home/('reporter_ed25519' if report else 'router_ed25519')
    known = home/('service_known_hosts' if report else 'router_known_hosts')
    # The service's existing AllowUsers admits warehouse only. This dedicated
    # key is forced to a fixed wrapper which drops to warehouse-observer.
    target = ('warehouse@'+SERVICE) if report else ('root@'+ROUTER)
    return ['/usr/bin/ssh', '-F', '/dev/null', '-i', str(key), '-b', CLOUD_SOURCE if report else SERVICE,
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile='+str(known), '-o', 'GlobalKnownHostsFile=/dev/null',
        '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
        '-o', 'KbdInteractiveAuthentication=no', '-o', 'HostKeyAlgorithms=ssh-ed25519',
        '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=1', target,
        'warehouse-report-v1' if report else 'warehouse-observe-v1']


def parse_router(text, expected_peer, now=None):
    now = time.time() if now is None else now
    names = ('WAN', 'ROUTES', 'HANDSHAKES', 'COUNTERS_OFFICE', 'COUNTERS_IOT', 'COUNTERS_WAN')
    markers = ['WH_OBSERVER_1', *names, 'END']
    lines = text.replace('\r', '').splitlines()
    if not lines or lines[0] != markers[0] or lines[-1] != 'END':
        raise ValueError('Router observation protocol is incomplete')
    sections = {}
    cursor = 1
    for index, name in enumerate(names):
        if cursor >= len(lines) or lines[cursor] != name:
            raise ValueError('Router observation section differs')
        cursor += 1
        end_marker = names[index+1] if index+1<len(names) else 'END'
        start = cursor
        while cursor < len(lines) and lines[cursor] != end_marker:
            cursor += 1
        if cursor >= len(lines):
            raise ValueError('Router observation section is truncated')
        sections[name] = '\n'.join(lines[start:cursor])
    wan = json.loads(sections['WAN'])
    if not isinstance(wan,dict): raise ValueError('Router WAN observation is invalid')
    routes = sections['ROUTES'].splitlines()
    defaults = [line for line in routes if line.startswith('default ')]
    route_present = any(re.search(r'\bdev\s+eth0(?:\s|$)', row) for row in defaults)
    counters = {}
    for role in ('OFFICE','IOT','WAN'):
        nft = json.loads(sections['COUNTERS_'+role])
        packets = 0
        for item in nft.get('nftables', []):
            rule = item.get('rule', {})
            if rule.get('chain') == 'forward_'+role.lower() and 'forward-deny' in rule.get('comment',''):
                for expr in rule.get('expr',[]):
                    packets += expr.get('counter',{}).get('packets',0)
        counters[role.lower()+'_denied_packets'] = packets
    entries = [line.split() for line in sections['HANDSHAKES'].splitlines() if line.strip()]
    matched = len(entries)==1 and len(entries[0])==2 and entries[0][0]==expected_peer
    stamp = int(entries[0][1]) if matched else 0
    recent = stamp>0 and 0<=now-stamp<=180
    vpn_route = any(re.match(r'10\.89\.0\.1(?:/32)?\s+dev\s+wg_warehouse(?:\s|$)', line) for line in routes)
    at = iso_time(now)
    return {'schema_version':1,'observer_id':'site','observed_at':at,'components':{
        'wan':component('healthy' if wan.get('up') is True and route_present else 'unhealthy',
            {'interface_up':wan.get('up') is True,'default_route_present':route_present,'counters':counters},at),
        'wireguard':component('healthy' if matched and recent and vpn_route else 'unhealthy',
            {'expected_peer_matches':matched,'latest_handshake_epoch':stamp,'handshake_age_seconds':round(now-stamp,2) if stamp else None,'peer_route_present':vpn_route},at)}}


def site_once(config, runner=run_command, clock=time.time):
    now = clock()
    expected = config.get('expected_peer', '')
    if not re.fullmatch(r'[A-Za-z0-9+/]{43}=', expected):
        raise ValueError('Site observer requires the expected cloud public key')
    try:
        snapshot = parse_router(runner(ssh_arguments(config)), expected, now)
    except Exception as error:
        at=iso_time(now)
        snapshot={'schema_version':1,'observer_id':'site','observed_at':at,'components':{
            name:component('unknown',{'error_type':type(error).__name__},at) for name in ('wan','wireguard')}}
    snapshot = validate_snapshot(snapshot,'site',now=now)
    atomic_json(Path(config.get('observations_dir', OBSERVATIONS))/'local.json', snapshot)
    return snapshot


def http_get(path):
    if not path.startswith('/api/') or '\n' in path or len(path)>300:
        raise ValueError('Only bounded service API paths are accepted')
    connection=http.client.HTTPConnection(SERVICE,8080,timeout=TIMEOUT,source_address=(CLOUD_SOURCE,0))
    try:
        connection.request('GET',path,headers={'Connection':'close','Accept':'application/json'})
        response=connection.getresponse()
        body=response.read(1048577)
        if len(body)>1048576:
            raise ValueError('Service API response exceeds limit')
        if response.status!=200:
            return {'connected':True,'status':response.status,'data':None}
        value=json.loads(body)
        if not isinstance(value,dict): raise ValueError('Service API JSON must be an object')
        return {'connected':True,'status':200,'data':value}
    finally:
        connection.close()


def parse_cloud_wireguard(value, expected_peer, now):
    if not isinstance(value,dict): raise ValueError('Cloud WireGuard observation is invalid')
    peers=value.get('peers',[])
    matched=peers==[expected_peer]
    handshakes=value.get('handshakes',{})
    stamp=handshakes.get(expected_peer,0) if isinstance(handshakes,dict) else 0
    recent=type(stamp) is int and stamp>0 and 0<=now-stamp<=180
    route=value.get('service_route_device')=='wg-warehouse'
    return component('healthy' if matched and recent and route else 'unhealthy',
        {'expected_peer_matches':matched,'latest_handshake_epoch':stamp,'handshake_age_seconds':round(now-stamp,2) if stamp else None,'service_route_uses_tunnel':route},iso_time(now))


def cloud_once(config, *, runner=run_command, getter=http_get, clock=time.time, sender=None):
    now=clock()
    at=iso_time(now)
    expected=config.get('expected_peer','')
    if not re.fullmatch(r'[A-Za-z0-9+/]{43}=',expected):
        raise ValueError('Cloud observer requires the expected router public key')
    try:
        api=getter('/api/health')
        reachable=api.get('status')==200 and isinstance(api.get('data'),dict)
        remote=component('healthy' if reachable else 'unhealthy',{'http_status':api.get('status'),'connected':api.get('connected') is True},at)
    except Exception as error:
        reachable=False
        remote=component('unhealthy',{'error_type':type(error).__name__},at)
    try:
        wg=json.loads(runner(['/usr/bin/sudo','-n','/usr/local/lib/warehouse-netops-observer/cloud-read']))
        wireguard=parse_cloud_wireguard(wg,expected,now)
    except Exception as error:
        wireguard=component('unknown',{'error_type':type(error).__name__},at)
    state=Path(config.get('state_dir','/var/lib/warehouse-netops-observer'))
    webhook=config.get('webhook',{'enabled':False})
    store=DeliveryStore(state/'outbox.sqlite3',clock=clock)
    try:
        store.observe_remote(reachable,webhook)
        remote['evidence']['recent_outages']=store.recent_remote_events()
        snapshot=validate_snapshot({'schema_version':1,'observer_id':'guangzhou','observed_at':at,
            'components':{'wireguard':wireguard,'remote_http':remote}},'guangzhou',now=now)
        atomic_json(state/'last-observation.json',snapshot)
        result={'snapshot':snapshot,'report_delivered':False,'notifications':{'enabled':False}}
        try:
            acknowledgement=json.loads(runner(ssh_arguments(config,report=True),data=json.dumps(snapshot,separators=(',',':')).encode()))
            result['report_delivered']=acknowledgement.get('accepted') is True
        except Exception as error:
            result['report_error_type']=type(error).__name__
        if reachable:
            try:
                after=store.cursor if store.initialized(webhook) else 0
                changes=getter('/api/event-changes?after_id='+str(after)+'&limit=200')
                if changes.get('status')==200:
                    store.accept_changes(changes['data'],webhook)
                else:
                    result['event_poll_status']=changes.get('status')
            except Exception as error:
                result['event_poll_error_type']=type(error).__name__
        result['notifications']=store.dispatch(webhook,**({'sender':sender} if sender else {}))
        store.daily_backup(state/'backups')
    finally:
        store.close()
    atomic_json(state/'status.json',{'observed_at':at,'report_delivered':result['report_delivered'],
        'remote_http_state':remote['state'],'notifications':result['notifications']},mode=0o600)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role',choices=('site','cloud'))
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true',help='One timer invocation (also the default)')
    args=parser.parse_args()
    try:
        config=private_config(args.config)
        result=site_once(config) if args.role=='site' else cloud_once(config)
        print(json.dumps({'role':args.role,'completed':True,
            'components':{key:row['state'] for key,row in result.get('snapshot',result)['components'].items()},
            **({'report_delivered':result['report_delivered'],'notifications':result['notifications']} if args.role=='cloud' else {})}))
        return 0
    except Exception as error:
        print(json.dumps({'role':args.role,'completed':False,'error_type':type(error).__name__}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
