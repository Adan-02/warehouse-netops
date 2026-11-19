"""Offline WAN-policy fault tests: no sockets, consoles or route mutations."""
import contextlib
import base64
import copy
import io
import json
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest import mock

try:
    from . import check_wan as wan
except ImportError:
    import check_wan as wan


def route(prefix, *, store='ActiveStore', next_hop='0.0.0.0', metric=10):
    return {'DestinationPrefix': prefix, 'NextHop': next_hop, 'InterfaceIndex': 42,
        'RouteMetric': metric, 'Protocol': 'NetMgmt', 'InstanceID': prefix + next_hop + str(metric), 'Store': store}


def snapshot():
    return {'addresses': [{'IPAddress': wan.SOURCE, 'PrefixLength': 24, 'InterfaceIndex': 42, 'AddressState': 'Preferred'}],
        'routes': [route('0.0.0.0/0', next_hop='192.168.1.1'), route('192.168.200.0/24')]}


class Routes:
    def __init__(self, *, conflict=None, denied=False, cleanup_failure=False):
        self.state = snapshot()
        if conflict:
            self.state['routes'].append(conflict)
        self.denied, self.cleanup_failure = denied, cleanup_failure
        self.added, self.removed = [], []

    def snapshot(self):
        return copy.deepcopy(self.state)

    def add(self, interface_index):
        if self.denied:
            raise wan.RouteError('Windows refused route creation.', needs_admin=True)
        owned = route(wan.PREFIX, next_hop=wan.GATEWAY, metric=23456)
        self.added.append(owned)
        self.state['routes'].append(owned)
        return owned

    def remove_owned(self, owned):
        self.removed.append(owned)
        if self.cleanup_failure:
            raise wan.RouteError('Route identity changed.')
        self.state['routes'].remove(owned)
        return {'owned_exact_route_absent': True, 'removed': True}


class Matrix:
    def __init__(self, *, extra_nic=False, missing_listener=False, no_direct_counter=False, rules_change=False):
        self.extra_nic, self.missing_listener = extra_nic, missing_listener
        self.no_direct_counter, self.rules_change = no_direct_counter, rules_change
        self.packets = 0
        self.closed = False

    def guest(self, role, purpose):
        if (role, purpose) != ('service', 'inventory'):
            raise AssertionError('Only the service inventory may be collected')
        nics = [{'name': 'ens33', 'up': True, 'addresses': ['192.168.30.10/24']}]
        if self.extra_nic:
            nics.append({'name': 'ens34', 'up': True, 'addresses': ['192.168.200.60/24']})
        return {'nics': nics, 'routes': [{'dst': 'default', 'gateway': '192.168.30.1', 'dev': 'ens33'}],
            'listeners': ['LISTEN 0 4096 192.168.30.10:' + str(port) + ' 0.0.0.0:*' for port in wan.PORTS if not (self.missing_listener and port == 8080)],
            'self_connect': {str(port): True for port in wan.PORTS}}

    def router(self):
        rules = [{'chain': 'forward_wan', 'handle': 1, 'comment': 'WAN deny',
            'expr': [{'counter': {'packets': 0 if self.no_direct_counter else self.packets}}, {'jump': {'target': 'handle_reject'}}]},
            {'chain': 'handle_reject', 'handle': 2, 'comment': 'reject', 'expr': [{'counter': {'packets': self.packets}}, {'reject': None}]}]
        if self.rules_change and self.packets:
            rules[0]['comment'] = 'changed during probe'
        return {'fw4_check_exit': 0, 'rules': rules}

    def close(self):
        self.closed = True


def verifier(matrix=None, routes=None, *, accept_port=None, probe_error=False):
    matrix, routes = matrix or Matrix(), routes or Routes()
    probed = []
    def probe(port):
        probed.append(port)
        if probe_error:
            raise OSError('host probe transport unavailable')
        matrix.packets += 1
        return {'source_ip': wan.SOURCE, 'port': port, 'connected': port == accept_port, 'errno': 10061}
    return wan.WanVerifier(matrix, routes, probe), probed


class WanTests(unittest.TestCase):
    def test_default_and_direct_routes_are_allowed_but_special_routes_refused(self):
        self.assertEqual(wan.conflicting_routes(snapshot()['routes']), [])
        for prefix in ('192.168.30.10/32', '192.168.30.0/24', '192.168.0.0/16', '128.0.0.0/1'):
            for store in ('ActiveStore', 'PersistentStore'):
                item = route(prefix, store=store)
                self.assertEqual(wan.conflicting_routes([item]), [item])

    def test_owned_route_exception_is_exact_and_only_active_store(self):
        owned = route(wan.PREFIX, next_hop=wan.GATEWAY)
        self.assertEqual(wan.conflicting_routes([owned], owned), [])
        changed = {**owned, 'RouteMetric': 999}
        self.assertEqual(wan.conflicting_routes([changed], owned), [changed])
        persistent = {**owned, 'Store': 'PersistentStore'}
        self.assertEqual(wan.conflicting_routes([persistent], owned), [persistent])

    def test_pass_requires_all_ports_forward_wan_counters_and_removes_only_owned(self):
        routes = Routes()
        original = routes.snapshot()
        runner, probed = verifier(routes=routes)
        report = runner.run()
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(probed, [22, 1883, 8080])
        self.assertEqual(routes.added, routes.removed)
        self.assertEqual(routes.snapshot(), original)
        port_checks = [item for item in report['checks'] if item['name'].startswith('wan_to_service_')]
        self.assertTrue(all(item['evidence']['forward_wan_deny_counter_increased'] for item in port_checks))

    def test_route_conflict_prevents_mutation_and_probes(self):
        routes = Routes(conflict=route('192.168.0.0/16', store='PersistentStore'))
        runner, probed = verifier(routes=routes)
        self.assertEqual(runner.run()['status'], 'inconclusive')
        self.assertEqual(routes.added, [])
        self.assertEqual(probed, [])

    def test_access_denied_reports_needs_admin_without_denial_conclusion(self):
        routes = Routes(denied=True)
        runner, probed = verifier(routes=routes)
        report = runner.run()
        self.assertEqual(report['status'], 'needs_admin')
        self.assertFalse(report['passed'])
        self.assertEqual(probed, [])
        self.assertEqual(routes.removed, [])
        self.assertFalse(any(item['name'].startswith('wan_to_service_') for item in report['checks']))

    def test_extra_service_nic_or_missing_listener_prevents_route_creation(self):
        for matrix in (Matrix(extra_nic=True), Matrix(missing_listener=True)):
            routes = Routes()
            runner, probed = verifier(matrix, routes)
            self.assertEqual(runner.run()['status'], 'inconclusive')
            self.assertEqual(routes.added, [])
            self.assertEqual(probed, [])

    def test_global_reject_counter_alone_does_not_prove_forward_wan(self):
        runner, _ = verifier(Matrix(no_direct_counter=True))
        report = runner.run()
        self.assertEqual(report['status'], 'inconclusive')
        self.assertFalse(report['passed'])

    def test_rules_changed_during_probe_do_not_pass(self):
        runner, _ = verifier(Matrix(rules_change=True))
        report = runner.run()
        self.assertEqual(report['status'], 'inconclusive')

    def test_unexpected_allowed_port_fails_but_cleanup_still_runs(self):
        routes = Routes()
        runner, _ = verifier(routes=routes, accept_port=1883)
        self.assertEqual(runner.run()['status'], 'failed')
        self.assertEqual(routes.removed, routes.added)

    def test_probe_exception_still_removes_temporary_route(self):
        routes = Routes()
        runner, _ = verifier(routes=routes, probe_error=True)
        self.assertEqual(runner.run()['status'], 'inconclusive')
        self.assertEqual(routes.removed, routes.added)

    def test_cleanup_failure_is_failure_even_if_all_denials_were_observed(self):
        routes = Routes(cleanup_failure=True)
        runner, _ = verifier(routes=routes)
        report = runner.run()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['checks'][-1]['name'], 'temporary_route_removed')
        self.assertEqual(report['checks'][-1]['status'], 'failed')

    def test_host_source_must_be_existing_preferred_correct_subnet(self):
        for change in ({'PrefixLength': 16}, {'AddressState': 'Tentative'}, {'IPAddress': '192.168.200.253'}):
            state = snapshot()
            state['addresses'][0].update(change)
            with self.assertRaises(wan.RouteError):
                wan.host_interface(state)

    def test_json_admin_refusal_is_preserved_from_powershell_result(self):
        result = subprocess.CompletedProcess([], 1, stdout='{"ok":false,"needs_admin":true,"error_type":"CimException"}', stderr='')
        with mock.patch.object(wan.sys, 'platform', 'win32'), mock.patch.object(wan.shutil, 'which', return_value='pwsh'), mock.patch.object(wan.subprocess, 'run', return_value=result):
            with self.assertRaises(wan.RouteError) as caught:
                wan.WindowsRoutes().execute('unused')
        self.assertTrue(caught.exception.needs_admin)

    @unittest.skipUnless(shutil.which('pwsh') or shutil.which('powershell'), 'PowerShell is unavailable')
    def test_only_exact_empty_persistent_store_query_is_accepted(self):
        # Execute the actual snapshot PowerShell with in-process fake cmdlets.
        # No Windows network query or mutation is made. ErrorRecord metadata
        # matches the observed NetRoute no-results exception, independently of
        # its localized message.
        bodies = []
        routes = wan.WindowsRoutes()
        with mock.patch.object(routes, 'execute', side_effect=lambda body: bodies.append(body)):
            routes.snapshot()
        for store, error_id, category, expected in (
            ('PersistentStore', 'CmdletizationQuery_NotFound_AddressFamily,Get-NetRoute', 'ObjectNotFound', True),
            ('PersistentStore', 'A_different_query_error,Get-NetRoute', 'ObjectNotFound', False),
            ('ActiveStore', 'CmdletizationQuery_NotFound_AddressFamily,Get-NetRoute', 'ObjectNotFound', False),
            ('PersistentStore', 'CmdletizationQuery_NotFound_AddressFamily,Get-NetRoute', 'PermissionDenied', False),
        ):
            fixture = """
class CimJobException : System.Exception { CimJobException() : base('模拟空集：localized message') {} }
function Get-NetIPAddress {
 param($AddressFamily)
 [pscustomobject]@{IPAddress='192.168.200.254';PrefixLength=24;InterfaceIndex=42;AddressState='Preferred'}
}
function Get-NetRoute {
 param($AddressFamily,$PolicyStore)
 if ($PolicyStore -eq 'STORE') {
   $record=[System.Management.Automation.ErrorRecord]::new([CimJobException]::new(),'ERROR_ID',[System.Management.Automation.ErrorCategory]::CATEGORY,$null)
   throw $record
 }
 [pscustomobject]@{DestinationPrefix='192.168.200.0/24';NextHop='0.0.0.0';InterfaceIndex=42;RouteMetric=1;Protocol='Local';InstanceID='fake'}
}
$ErrorActionPreference='Stop'
try {
""".replace('STORE', store).replace('ERROR_ID', error_id).replace('CATEGORY', category)
            script = fixture + bodies[0] + "\n} catch { @{ok=$false;error_id=$_.FullyQualifiedErrorId} | ConvertTo-Json -Compress }"
            encoded = base64.b64encode(script.encode('utf-16le')).decode()
            result = subprocess.run([shutil.which('pwsh') or shutil.which('powershell'), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            response = json.loads(result.stdout.lstrip('\ufeff'))
            self.assertEqual(response['ok'], expected, (store, error_id, category, response))
            if expected:
                self.assertTrue(response['routes'])
                self.assertTrue(all(row['Store'] == 'ActiveStore' for row in response['routes']))

    def test_main_writes_report_and_closes_console_runner_offline(self):
        matrix, routes = Matrix(), Routes(denied=True)
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            target = Path(temporary) / 'evidence.json'
            result = wan.main(['--output', str(target)], matrix=matrix, routes=routes, probe=lambda port: self.fail('must not probe'))
            self.assertEqual(result, 3)
            self.assertEqual(json.loads(target.read_text())['status'], 'needs_admin')
        self.assertTrue(matrix.closed)


if __name__ == '__main__':
    unittest.main()
