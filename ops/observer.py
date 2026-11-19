"""One-shot observers for systemd timers; no loops or overlapping probe jobs."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import re
import subprocess
import time

from .common import MAX_SNAPSHOT_BYTES, atomic_json, component, iso_time, parse_time, private_config, validate_snapshot
from .paths import OBSERVATIONS_DIR, directory, observer_id, observer_paths
from .webhook import NotificationState

ROUTER = '192.168.30.1'
SERVICE = '192.168.30.10'
GUANGZHOU_SOURCE = '10.89.0.1'
CLOUD_SOURCE = GUANGZHOU_SOURCE  # Existing imports remain valid.
OBSERVATIONS = OBSERVATIONS_DIR.as_posix()
TIMEOUT = 3
STAGES = ('probe', 'reachability', 'report', 'event_sync', 'dispatch', 'maintenance')


def run_command(arguments, *, data=None, timeout=TIMEOUT):
    """No shell, no credential-bearing command line, bounded trusted output."""
    result = subprocess.run(arguments, input=data, capture_output=True, timeout=timeout)
    if len(result.stdout) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Fixed observer command returned oversized output')
    if result.returncode:
        raise RuntimeError('Fixed observer command failed')
    return result.stdout.decode('utf-8', errors='strict')


def _fixed_ssh(config, *, key_name, known_name, source, target, protocol):
    home = directory(observer_paths(config).private_dir, 'private_dir')
    return ['/usr/bin/ssh', '-F', '/dev/null', '-i', str(home/key_name), '-b', source,
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile='+str(home/known_name), '-o', 'GlobalKnownHostsFile=/dev/null',
        '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
        '-o', 'KbdInteractiveAuthentication=no', '-o', 'HostKeyAlgorithms=ssh-ed25519',
        '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=1', target,
        protocol]


def router_observation_ssh(config):
    """Site service host -> router, fixed read-only observer key and command."""
    return _fixed_ssh(config, key_name='router_ed25519', known_name='router_known_hosts',
        source=SERVICE, target='root@'+ROUTER, protocol='warehouse-observe-v1')


def snapshot_report_ssh(config):
    """Guangzhou -> service's fixed wrapper, then unprivileged receiver UID."""
    return _fixed_ssh(config, key_name='reporter_ed25519', known_name='service_known_hosts',
        source=GUANGZHOU_SOURCE, target='warehouse@'+SERVICE, protocol='warehouse-report-v1')


def ssh_arguments(config, *, report=False):
    """Compatibility wrapper; new code names the intended SSH capability."""
    return snapshot_report_ssh(config) if report else router_observation_ssh(config)


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
    paths = observer_paths(config)
    status_path = Path(paths.observations_dir)/'site-status.json'
    previous = previous_stage_successes(status_path)
    expected = config.get('expected_peer', '')
    if not re.fullmatch(r'[A-Za-z0-9+/]{43}=', expected):
        raise ValueError('Site observer requires the expected cloud public key')
    try:
        snapshot = parse_router(runner(router_observation_ssh(config)), expected, now)
    except Exception as error:
        at=iso_time(now)
        snapshot={'schema_version':1,'observer_id':'site','observed_at':at,'components':{
            name:component('unknown',{'error_type':type(error).__name__},at) for name in ('wan','wireguard')}}
    snapshot = validate_snapshot(snapshot,'site',now=now)
    atomic_json(Path(paths.observations_dir)/'local.json', snapshot)
    states = {row['state'] for row in snapshot['components'].values()}
    state = 'unhealthy' if 'unhealthy' in states else 'unknown' if 'unknown' in states else 'healthy'
    stages = {'probe': stage_result(state, 'ok' if state == 'healthy' else 'router_observation_unavailable' if state == 'unknown' else 'router_network_unhealthy', now, previous.get('probe'))}
    atomic_json(status_path, {'schema_version': 1, 'observer_id': 'site', 'observed_at': iso_time(now), 'stages': stages})
    return snapshot


def http_get(path):
    if not path.startswith('/api/') or '\n' in path or len(path)>300:
        raise ValueError('Only bounded service API paths are accepted')
    connection=http.client.HTTPConnection(SERVICE,8080,timeout=TIMEOUT,source_address=(GUANGZHOU_SOURCE,0))
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


def parse_guangzhou_wireguard(value, expected_peer, now):
    if not isinstance(value,dict): raise ValueError('Guangzhou WireGuard observation is invalid')
    peers=value.get('peers',[])
    matched=peers==[expected_peer]
    handshakes=value.get('handshakes',{})
    stamp=handshakes.get(expected_peer,0) if isinstance(handshakes,dict) else 0
    recent=type(stamp) is int and stamp>0 and 0<=now-stamp<=180
    route=value.get('service_route_device')=='wg-warehouse'
    return component('healthy' if matched and recent and route else 'unhealthy',
        {'expected_peer_matches':matched,'latest_handshake_epoch':stamp,'handshake_age_seconds':round(now-stamp,2) if stamp else None,'service_route_uses_tunnel':route},iso_time(now))


def previous_stage_successes(path):
    """Read only bounded success timestamps, never echo persisted diagnostics."""
    try:
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        stages = value.get('stages', {})
        result = {}
        for name in STAGES:
            stamp = stages.get(name, {}).get('last_success_at')
            if stamp is not None:
                parse_time(stamp)
                result[name] = stamp
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def stage_result(state, reason_code, now, last_success_at=None):
    return {'state': state, 'reason_code': reason_code,
        'last_success_at': iso_time(now) if state == 'healthy' else last_success_at}


def probe_guangzhou(expected_peer, runner, getter, now):
    """Probe independently: a failed HTTP request cannot hide local WG state."""
    at = iso_time(now)
    try:
        api=getter('/api/health')
        reachable=api.get('status')==200 and isinstance(api.get('data'),dict)
        remote=component('healthy' if reachable else 'unhealthy',{'http_status':api.get('status'),'connected':api.get('connected') is True},at)
    except Exception as error:
        reachable=False
        remote=component('unhealthy',{'error_type':type(error).__name__},at)
    try:
        wg=json.loads(runner(['/usr/bin/sudo','-n','/usr/local/lib/warehouse-netops-observer/cloud-read']))
        wireguard=parse_guangzhou_wireguard(wg,expected_peer,now)
    except Exception as error:
        wireguard=component('unknown',{'error_type':type(error).__name__},at)
    return reachable, {'wireguard': wireguard, 'remote_http': remote}


def record_reachability(store, reachable, webhook, remote_component):
    store.observe_remote(reachable, webhook)
    remote_component['evidence']['recent_outages'] = store.recent_remote_events()


def report_snapshot(config, snapshot, runner):
    acknowledgement = json.loads(runner(snapshot_report_ssh(config),
        data=json.dumps(snapshot, separators=(',', ':')).encode()))
    return acknowledgement.get('accepted') is True


def sync_event_changes(store, webhook, getter):
    after = store.cursor if store.initialized(webhook) else 0
    changes = getter('/api/event-changes?after_id='+str(after)+'&limit=200')
    if changes.get('status') != 200:
        return False, changes.get('status')
    store.accept_changes(changes['data'], webhook)
    return True, 200


def dispatch_notifications(store, webhook, sender):
    return store.dispatch(webhook, **({'sender': sender} if sender else {}))


def maintain_notification_state(store, private_dir):
    return store.daily_backup(Path(private_dir)/'backups')


def guangzhou_once(config, *, runner=run_command, getter=http_get, clock=time.time, sender=None):
    """One explicit cycle; failed reporting never blocks event sync or retry."""
    now = clock()
    at = iso_time(now)
    expected = config.get('expected_peer', '')
    if not re.fullmatch(r'[A-Za-z0-9+/]{43}=', expected):
        raise ValueError('Guangzhou observer requires the expected router public key')
    paths = observer_paths(config)
    private_dir = Path(paths.private_dir)
    previous = previous_stage_successes(private_dir/'status.json')
    stages = {}
    def stage(name, state, reason):
        stages[name] = stage_result(state, reason, now, previous.get(name))

    reachable, components = probe_guangzhou(expected, runner, getter, now)
    states = {row['state'] for row in components.values()}
    probe_state = 'unhealthy' if 'unhealthy' in states else 'unknown' if 'unknown' in states else 'healthy'
    stage('probe', probe_state, 'ok' if probe_state == 'healthy' else 'service_unreachable' if not reachable else 'wireguard_unavailable')

    webhook = config.get('webhook', {'enabled': False})
    result = {'report_delivered': False, 'notifications': {'enabled': False}, 'stages': stages}
    store = None
    try:
        try:
            store = NotificationState(private_dir/'outbox.sqlite3', clock=clock)
            record_reachability(store, reachable, webhook, components['remote_http'])
            stage('reachability', 'healthy', 'recorded')
        except Exception:
            stage('reachability', 'unhealthy', 'notification_state_unavailable')

        snapshot = validate_snapshot({'schema_version': 1, 'observer_id': 'guangzhou',
            'observed_at': at, 'components': components}, 'guangzhou', now=now)
        result['snapshot'] = snapshot
        try:
            result['report_delivered'] = report_snapshot(config, snapshot, runner)
            stage('report', 'healthy' if result['report_delivered'] else 'unhealthy',
                'accepted' if result['report_delivered'] else 'receiver_rejected')
        except Exception as error:
            result['report_error_type'] = type(error).__name__
            stage('report', 'unhealthy', 'report_transport_failed')

        if store is None:
            for name in ('event_sync', 'dispatch', 'maintenance'):
                stage(name, 'unknown', 'notification_state_unavailable')
        else:
            if not reachable:
                stage('event_sync', 'skipped', 'service_unreachable')
            else:
                try:
                    accepted, status = sync_event_changes(store, webhook, getter)
                    if not accepted:
                        result['event_poll_status'] = status
                    stage('event_sync', 'healthy' if accepted else 'unhealthy',
                        'synchronized' if accepted else 'event_http_not_200')
                except Exception as error:
                    result['event_poll_error_type'] = type(error).__name__
                    stage('event_sync', 'unhealthy', 'event_sync_failed')
            try:
                result['notifications'] = dispatch_notifications(store, webhook, sender)
                if not result['notifications']['enabled']:
                    stage('dispatch', 'disabled', 'webhook_disabled')
                else:
                    counts = result['notifications'].get('deliveries', {})
                    state = 'unhealthy' if counts.get('failed') else 'unknown' if counts.get('retry') else 'healthy'
                    stage('dispatch', state, 'delivery_failed' if state == 'unhealthy' else 'retry_pending' if state == 'unknown' else 'completed')
            except Exception:
                stage('dispatch', 'unhealthy', 'dispatch_failed')
            try:
                maintain_notification_state(store, paths.private_dir)
                stage('maintenance', 'healthy', 'completed')
            except Exception:
                stage('maintenance', 'unhealthy', 'backup_failed')
    finally:
        if store is not None:
            store.close()
    atomic_json(private_dir/'last-observation.json', snapshot)
    atomic_json(private_dir/'status.json', {'schema_version': 1, 'observer_id': 'guangzhou',
        'observed_at': at, 'report_delivered': result['report_delivered'],
        'remote_http_state': components['remote_http']['state'], 'notifications': result['notifications'],
        'stages': stages}, mode=0o600)
    return result


# Existing Python imports, CLI role, systemd filenames and snapshot identity stay compatible.
cloud_once = guangzhou_once
parse_cloud_wireguard = parse_guangzhou_wireguard


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role',choices=('site','guangzhou','cloud'))
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true',help='One timer invocation (also the default)')
    args=parser.parse_args()
    try:
        config=private_config(args.config)
        location = observer_id(args.role)
        if location == 'site':
            result = site_once(config)
            status_path = Path(observer_paths(config).observations_dir)/'site-status.json'
            stages = json.loads(status_path.read_text())['stages']
        else:
            result = guangzhou_once(config)
            stages = result['stages']
        print(json.dumps({'role':args.role,'observer_id':location,'completed':True,'stages':stages,
            'components':{key:row['state'] for key,row in result.get('snapshot',result)['components'].items()},
            **({'report_delivered':result['report_delivered'],'notifications':result['notifications']} if location=='guangzhou' else {})}))
        return 0
    except Exception as error:
        print(json.dumps({'role':args.role,'completed':False,'error_type':type(error).__name__}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
