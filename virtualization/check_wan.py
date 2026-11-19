"""Verify three exact host-to-service TCP denials through the OpenWrt WAN.

Only a temporary Windows ActiveStore /32 is added. No default route, cloud,
WireGuard configuration, existing route or firewall rule is modified.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

try:
    from .check_matrix import MatrixRunner, PORTS, deny_counter_evidence, network_ready
except ImportError:
    from check_matrix import MatrixRunner, PORTS, deny_counter_evidence, network_ready

SOURCE = '192.168.200.254'
GATEWAY = '192.168.200.50'
DESTINATION = '192.168.30.10'
PREFIX = DESTINATION + '/32'
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / 'evidence/virtual-wan-policy.json'
ROUTE_KEYS = ('DestinationPrefix', 'NextHop', 'InterfaceIndex', 'RouteMetric', 'Protocol', 'InstanceID')


class RouteError(Exception):
    def __init__(self, message, *, needs_admin=False):
        super().__init__(message)
        self.needs_admin = needs_admin


def route_identity(route):
    return {key: route.get(key) for key in ROUTE_KEYS}


def conflicting_routes(routes, owned=None):
    conflicts = []
    for route in routes:
        try:
            network = ipaddress.IPv4Network(route['DestinationPrefix'], strict=False)
        except (KeyError, ValueError):
            raise RouteError('Windows returned an invalid IPv4 route; no route was changed.') from None
        if network.prefixlen == 0 or ipaddress.IPv4Address(DESTINATION) not in network:
            continue
        if owned is not None and route.get('Store') == 'ActiveStore' and route_identity(route) == route_identity(owned):
            continue
        conflicts.append(route)
    return conflicts


def host_interface(snapshot):
    addresses = [row for row in snapshot.get('addresses', []) if row.get('IPAddress') == SOURCE]
    if len(addresses) != 1 or addresses[0].get('PrefixLength') != 24 or addresses[0].get('AddressState') != 'Preferred':
        raise RouteError('Expected exactly one preferred existing 192.168.200.254/24 interface.')
    index = addresses[0]['InterfaceIndex']
    direct = [row for row in snapshot.get('routes', []) if row.get('Store') == 'ActiveStore'
        and row.get('DestinationPrefix') == '192.168.200.0/24'
        and row.get('NextHop') == '0.0.0.0' and row.get('InterfaceIndex') == index]
    if not direct:
        raise RouteError('The existing VMnet8 interface has no direct 192.168.200.0/24 route.')
    return {'address': SOURCE, 'prefix_length': 24, 'interface_index': index, 'direct_route': direct[0]}


class WindowsRoutes:
    """Structured PowerShell calls; creation/removal never requests elevation."""
    def execute(self, body):
        if sys.platform != 'win32':
            raise RouteError('This verifier requires the Windows host with its existing VMnet8 adapter.')
        executable = shutil.which('pwsh') or shutil.which('powershell')
        if not executable:
            raise RouteError('PowerShell is unavailable; no route was changed.')
        script = "$ErrorActionPreference='Stop'; try {\n" + body + "\n} catch {\n" + """
 $denied = $_.CategoryInfo.Category -eq 'PermissionDenied' -or
   $_.Exception -is [System.UnauthorizedAccessException] -or
   $_.Exception.HResult -eq -2147024891 -or $_.Exception.NativeErrorCode -eq 5
 @{ok=$false;needs_admin=[bool]$denied;error_type=$_.Exception.GetType().Name;error_id=$_.FullyQualifiedErrorId} | ConvertTo-Json -Compress -Depth 8
 exit 1
}
"""
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
        try:
            value = json.loads(result.stdout.lstrip('\ufeff'))
        except ValueError:
            raise RouteError('PowerShell did not return a complete route-operation result.') from None
        if result.returncode or value.get('ok') is not True:
            raise RouteError('Windows refused the route operation (' + str(value.get('error_type', 'unknown')) + ').',
                needs_admin=value.get('needs_admin') is True)
        return value

    def snapshot(self):
        return self.execute("""
 $addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq '192.168.200.254' |
   Select-Object IPAddress,PrefixLength,InterfaceIndex,@{n='AddressState';e={[string]$_.AddressState}})
 $routes = @(foreach ($store in @('ActiveStore','PersistentStore')) {
   try {
     Get-NetRoute -AddressFamily IPv4 -PolicyStore $store | Select-Object DestinationPrefix,NextHop,InterfaceIndex,RouteMetric,
       @{n='Protocol';e={[string]$_.Protocol}},InstanceID,@{n='Store';e={$store}}
   } catch {
     # An empty PersistentStore is normal. Suppress only this exact IPv4
     # no-results error; access failures and other query failures must escape.
     if (-not ($store -eq 'PersistentStore' -and
       $_.FullyQualifiedErrorId -eq 'CmdletizationQuery_NotFound_AddressFamily,Get-NetRoute' -and
       $_.CategoryInfo.Category -eq 'ObjectNotFound' -and
       $_.Exception.GetType().Name -eq 'CimJobException')) { throw }
   }
 })
 @{ok=$true;addresses=$addresses;routes=$routes} | ConvertTo-Json -Compress -Depth 8
""")

    def add(self, interface_index):
        if not isinstance(interface_index, int) or interface_index < 1:
            raise RouteError('Invalid interface index; no route was changed.')
        # A random metric makes the exact object created by this process
        # distinguishable during cleanup. /32 wins over any default metric.
        metric = 20000 + uuid.uuid4().int % 40000
        value = self.execute(f"""
 $created = New-NetRoute -DestinationPrefix '192.168.30.10/32' -NextHop '192.168.200.50' -InterfaceIndex {interface_index} -RouteMetric {metric} -PolicyStore ActiveStore
 $route = $created | Select-Object DestinationPrefix,NextHop,InterfaceIndex,RouteMetric,@{{n='Protocol';e={{[string]$_.Protocol}}}},InstanceID
 @{{ok=$true;route=$route}} | ConvertTo-Json -Compress -Depth 8
""")
        route = value.get('route')
        if not isinstance(route, dict) or route.get('DestinationPrefix') != PREFIX or route.get('NextHop') != GATEWAY or route.get('InterfaceIndex') != interface_index or route.get('RouteMetric') != metric:
            raise RouteError('Windows returned an unexpected newly created route identity; inspect the exact /32 manually.')
        return route

    def remove_owned(self, owned):
        # Supply the complete identity as encoded JSON, never shell fragments.
        payload = base64.b64encode(json.dumps(route_identity(owned)).encode()).decode()
        return self.execute("""
 $owned = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('""" + payload + """')) | ConvertFrom-Json
 $matching = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore | Where-Object {
   $_.DestinationPrefix -eq $owned.DestinationPrefix -and $_.NextHop -eq $owned.NextHop -and $_.InterfaceIndex -eq $owned.InterfaceIndex
 })
 $same = @($matching | Where-Object {
   $_.RouteMetric -eq $owned.RouteMetric -and [string]$_.Protocol -eq $owned.Protocol -and $_.InstanceID -eq $owned.InstanceID
 })
 if ($matching.Count -ne $same.Count -or $same.Count -gt 1) { throw 'Route identity changed; refusing to delete another route.' }
 if ($same.Count -eq 1) { $same[0] | Remove-NetRoute -Confirm:$false }
 $remaining = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore | Where-Object {
   $_.DestinationPrefix -eq $owned.DestinationPrefix -and $_.NextHop -eq $owned.NextHop -and $_.InterfaceIndex -eq $owned.InterfaceIndex
 })
 if ($remaining.Count -ne 0) { throw 'Temporary exact route is still present.' }
 @{ok=$true;removed=($same.Count -eq 1);owned_exact_route_absent=$true} | ConvertTo-Json -Compress -Depth 8
""")


def tcp_probe(port):
    if port not in PORTS:
        raise ValueError('Only the three specified service ports may be probed.')
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    row = {'connected': False, 'source_ip': None, 'destination': DESTINATION, 'port': port}
    start = time.monotonic()
    try:
        sock.bind((SOURCE, 0))
        row['source_ip'] = sock.getsockname()[0]
        sock.connect((DESTINATION, port))
        row['connected'] = True
    except OSError as error:
        row.update(errno=error.errno, winerror=getattr(error, 'winerror', None), error_type=type(error).__name__)
    finally:
        sock.close()
    row['elapsed_ms'] = round((time.monotonic() - start) * 1000, 1)
    return row


def listeners_ready(inventory):
    endpoints = [line.split()[3] for line in inventory.get('listeners', []) if len(line.split()) >= 4]
    return all(inventory.get('self_connect', {}).get(str(port)) is True
        and any(endpoint.rsplit(':', 1)[-1] == str(port) for endpoint in endpoints) for port in PORTS)


class WanVerifier:
    def __init__(self, matrix, routes, probe=tcp_probe):
        self.matrix, self.routes, self.probe = matrix, routes, probe
        self.owned = None

    def run(self):
        report = {'observed_at': datetime.now(timezone.utc).isoformat(), 'phase': 'wan-policy',
            'source': SOURCE, 'gateway': GATEWAY, 'destination': DESTINATION, 'ports': list(PORTS),
            'scope': 'Windows VMnet8 host through OpenWrt WAN to the single-NIC service VM',
            'checks': [], 'cloud_or_us_accessed': False, 'router_rules_mutated': False,
            'not_verified': ['physical WAN', 'Internet-origin traffic', 'WireGuard remote policy']}
        def add(name, status, evidence):
            report['checks'].append({'name': name, 'status': status, 'evidence': evidence})
        try:
            self._checks(report, add)
        except RouteError as error:
            add('windows_route_operation', 'needs_admin' if error.needs_admin else 'inconclusive', {'detail': str(error)})
        except Exception as error:
            add('evidence_collection', 'inconclusive', {'error_type': type(error).__name__, 'detail': 'No refusal conclusion is drawn from missing transport evidence.'})
        finally:
            if self.owned is not None:
                try:
                    cleanup = self.routes.remove_owned(self.owned)
                    add('temporary_route_removed', 'pass' if cleanup.get('owned_exact_route_absent') is True else 'failed', cleanup)
                except Exception as error:
                    add('temporary_route_removed', 'failed', {'error_type': type(error).__name__, 'owned_route': route_identity(self.owned), 'action': 'Inspect this exact temporary route; no broad removal was attempted.'})
        statuses = [item['status'] for item in report['checks']]
        report['passed'] = bool(statuses) and all(item == 'pass' for item in statuses)
        report['status'] = 'passed' if report['passed'] else 'failed' if 'failed' in statuses else 'needs_admin' if 'needs_admin' in statuses else 'inconclusive'
        report['interpretation'] = 'A TCP denial requires the correct bound source, verified service listeners, a verified temporary route, unchanged fw4 rules and an increased deny counter directly in forward_wan for that port. A timeout alone proves nothing.'
        return report

    def _checks(self, report, add):
        snapshot = self.routes.snapshot()
        interface = host_interface(snapshot)
        add('existing_vmnet8_source', 'pass', interface)
        conflicts = conflicting_routes(snapshot['routes'])
        add('no_existing_destination_route', 'inconclusive' if conflicts else 'pass', {'conflicts': conflicts})
        if conflicts:
            return
        inventory = self.matrix.guest('service', 'inventory')
        ready = network_ready('service', inventory) and listeners_ready(inventory)
        add('service_single_nic_and_listeners', 'pass' if ready else 'inconclusive', inventory)
        before = self.matrix.router()
        add('router_fw4_check', 'pass' if before.get('fw4_check_exit') == 0 else 'failed', before)
        if not ready or before.get('fw4_check_exit') != 0:
            return
        # Recheck immediately before mutation: do not replace a concurrently
        # added route even if the first inventory was clear.
        fresh = self.routes.snapshot()
        if host_interface(fresh)['interface_index'] != interface['interface_index'] or conflicting_routes(fresh['routes']):
            add('route_preflight_recheck', 'inconclusive', {'detail': 'Source interface or destination routes changed; nothing added.'})
            return
        self.owned = self.routes.add(interface['interface_index'])
        current = self.routes.snapshot()
        matching = [row for row in current['routes'] if row.get('Store') == 'ActiveStore' and route_identity(row) == route_identity(self.owned)]
        valid = len(matching) == 1 and not conflicting_routes(current['routes'], self.owned)
        add('temporary_exact_route', 'pass' if valid else 'inconclusive', {'created_route': self.owned, 'verified_active': valid})
        if not valid:
            return
        for port in PORTS:
            before = self.matrix.router()
            row = self.probe(port)
            after = self.matrix.router()
            deny = deny_counter_evidence(before['rules'], after['rules'], 'wan')
            direct = any(item['chain'] == 'forward_wan' and item['delta'] > 0 for item in deny['deltas'])
            host_error = row.get('errno') in (13, 99, 101, 113, 10013, 10049, 10051, 10065) or row.get('winerror') in (10013, 10049, 10051, 10065)
            if row.get('source_ip') != SOURCE or not isinstance(row.get('connected'), bool) or host_error or before.get('fw4_check_exit') != 0 or after.get('fw4_check_exit') != 0:
                status = 'inconclusive'
            elif row['connected']:
                status = 'failed'
            elif not deny['verified'] or not direct:
                status = 'inconclusive'
            else:
                status = 'pass'
            add('wan_to_service_' + str(port), status, {'expected_connect': False, **row,
                'target_listening_verified': True, 'counter_evidence': deny, 'forward_wan_deny_counter_increased': direct})


def main(argv=None, *, matrix=None, routes=None, probe=tcp_probe):
    parser = argparse.ArgumentParser(description='Verify exact VMnet8 WAN denials with an automatically removed ActiveStore /32 route')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if matrix is None:
        try:
            from .console import Console
        except ImportError:
            from console import Console
        matrix = MatrixRunner(Console)
    try:
        report = WanVerifier(matrix, routes or WindowsRoutes(), probe).run()
    finally:
        matrix.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status':report['status'],'passed':report['passed'],
        'checks':[{'name':row['name'],'status':row['status']} for row in report['checks']],
        'evidence':str(args.output)}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1 if report['status'] == 'failed' else 3 if report['status'] == 'needs_admin' else 2


if __name__ == '__main__':
    sys.exit(main())
