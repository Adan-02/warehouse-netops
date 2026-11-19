"""Pure parser/fake-console tests: no VM, cloud, or firewall is contacted."""
import copy
import json
import re
import unittest

try:
    from . import check_paths as paths
    from .check_matrix import rules_fingerprint
except ImportError:
    import check_paths as paths
    from check_matrix import rules_fingerprint


def inventory(role):
    return {'role':role, 'nics':[{'name':'eth0','up':True,'addresses':[paths.ADDRESSES[role]+'/24']}], 'routes':[{'dst':'default','gateway':'192.168.'+('10' if role=='office' else '20')+'.1','dev':'eth0'}]}


class FakeMatrix:
    def __init__(self, owner):
        self.owner = owner

    def guest(self, role, purpose):
        row = inventory(role)
        if self.owner.bad_inventory and role == 'iot':
            row['nics'].append({'name':'eth1','up':True,'addresses':['192.168.200.61/24']})
        return row

    def router(self):
        rules = [{'chain':'forward_'+role,'handle':index+1,'comment':role+' drop','expr':[{'counter':{'packets':self.owner.counters[role]}},{'drop':None}]} for index,role in enumerate(('office','iot'))]
        return {'fw4_check_exit':0,'rules':rules,'fingerprint':rules_fingerprint(rules)}

    def close(self):
        pass


class FakePaths(paths.PathsRunner):
    def __init__(self):
        super().__init__(lambda role: None, sleep=lambda seconds: None)
        self.matrix = FakeMatrix(self)
        self.units = {}
        self.counters = {'office':0,'iot':0}
        self.commands = []
        self.cross_probes = []
        self.bad_inventory = False
        self.guard_busy_role = None
        self.no_counter = False
        self.cross_allowed = False
        self.start_timeout_role = None
        self.cleanup_failure_role = None
        self.expire_listener = False
        self.empty_broker_logs = False
        self.broker_source = paths.ADDRESSES['iot']
        self.api_seq = 10
        self.iot_pid_changes = False
        self.iot_reads = 0

    def command(self, role, command, timeout=30):
        self.commands.append((role, command))
        match = re.search(r'--unit=(warehouse-path-listener-[0-9a-f]{12}-(?:office|iot))', command)
        if match:
            self.units[role] = {'name':match.group(1),'active':True,'pid':101 if role=='office' else 102}
            if self.start_timeout_role == role:
                raise TimeoutError('private exception must not be exported')
        return ''

    def guest_json(self, role, script, timeout=30):
        if 'puback_records(records,pid,time.time())' in script:
            self.iot_reads += 1
            pid = 123 if not self.iot_pid_changes or self.iot_reads == 1 else 124
            return {'main_pid':pid,'unit_active':True,'source_argument_matches':True,'publisher_script_matches':True,'journal_exit':0,
                    'pubacks':[{'device_id':'cabinet-01','seq':10,'temperature_c':25.0,'humidity_pct':50.0,'accepted_epoch':999,'age_seconds':1}],
                    'mqtt_sockets':[{'source_ip':paths.ADDRESSES['iot'],'source_port':40000,'peer_ip':paths.ADDRESSES['service'],'peer_port':1883,'main_pid_matches':True}]}
        if "'logs','--no-color'" in script:
            return {'command_exit':0,'connections':[] if self.empty_broker_logs else [{'source_ip':self.broker_source,'source_port':40000,'client_id':'warehouse-sim-abcdef123456','username':'cabinet-01'}], 'window':'test fixture'}
        if 'HTTPConnection' in script:
            return {'source_ip':paths.ADDRESSES['office'],'connected':True,'status':200,'data':{'device_id':'cabinet-01','seq':self.api_seq,'temperature_c':25.0,'humidity_pct':50.0,'broker_connected':True,'demo_mode':True,'data_status':'online'}}
        if 'unit=' in script:
            if "'systemctl','stop'" in script:
                if self.cleanup_failure_role == role:
                    raise TimeoutError('private exception must not be exported')
                if role in self.units:
                    self.units[role]['active'] = False
            unit = self.units.get(role)
            active = unit is not None and unit['active']
            return {'load_state':'loaded' if active else 'not-found','active_state':'active' if active else 'inactive','sub_state':'running' if active else 'dead','main_pid':unit['pid'] if active else 0,'port_listening':active or (role==self.guard_busy_role and unit is None),'address_listening':active,'ss_exit':0}
        if "'target_ip':" in script:
            source = re.search(r"s.bind\(\('([^']+)'", script).group(1)
            target = re.search(r"'target_ip':'([^']+)'", script).group(1)
            if source == target:
                connected = self.units.get(role,{}).get('active',False)
            else:
                self.cross_probes.append((role, source, target))
                connected = self.cross_allowed
                if not self.no_counter:
                    self.counters[role] += 1
                if self.expire_listener:
                    target_role = 'iot' if role=='office' else 'office'
                    self.units[target_role]['active'] = False
            return {'source_ip':source,'target_ip':target,'port':9000,'connected':connected,'errno':None if connected else 113}
        raise AssertionError('Unknown test script')


class PathsTests(unittest.TestCase):
    def payload(self, **changes):
        value = {'device_id':'cabinet-01','seq':10,'temperature_c':25.0,'humidity_pct':50.0,'private_field':'must disappear'}
        value.update(changes)
        return value

    def journal_record(self, pid='123', age=1, **changes):
        return {'_PID':pid,'__REALTIME_TIMESTAMP':str(int((1000-age)*1_000_000)), 'MESSAGE':'SIMULATED PUBACK accepted '+json.dumps(self.payload(**changes))}

    def test_puback_parser_current_pid_recent_and_public_only(self):
        records = [self.journal_record(),self.journal_record(pid='124'),self.journal_record(age=21),self.journal_record(age=-2),self.journal_record(seq=True),{'_PID':'123','MESSAGE':'failure password=secret','__REALTIME_TIMESTAMP':'999000000'}]
        result = paths.puback_records(records,123,1000)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['seq'],10)
        self.assertNotIn('private_field',result[0])
        self.assertNotIn('secret',json.dumps(result))

    def test_invalid_payloads_are_not_identity_evidence(self):
        for changes in ({'seq':True},{'seq':0},{'humidity_pct':101},{'temperature_c':float('nan')},{'device_id':'different'}):
            self.assertIsNone(paths.public_payload(self.payload(**changes)))

    def test_broker_notice_extracts_authenticated_source(self):
        line = "broker-1 | 2026-11-19T09:00:00.000000Z 123: New client connected from 192.168.20.100:40000 as warehouse-sim-abcdef123456 (p5, c1, k30, u'cabinet-01')."
        result = paths.broker_connections([line])
        self.assertEqual(result,[{'source_ip':'192.168.20.100','source_port':40000,'client_id':'warehouse-sim-abcdef123456','username':'cabinet-01','notice':'New client connected'}])
        self.assertEqual(paths.broker_connections([line.replace("u'cabinet-01'", "u'wrong-user'")]),[])
        self.assertEqual(paths.broker_connections(['Unknown format password=secret']),[])

    def test_identity_requires_all_correlated_evidence(self):
        report = FakePaths().identity()
        self.assertTrue(report['passed'])
        self.assertFalse(report['cloud_or_us_accessed'])
        self.assertEqual(report['temporary_units'],[])

    def test_unknown_broker_logs_stay_inconclusive(self):
        runner = FakePaths()
        runner.empty_broker_logs = True
        self.assertEqual(runner.identity()['status'],'inconclusive')
        self.assertEqual(runner.commands,[])

    def test_nat_source_without_current_port_match_is_inconclusive(self):
        runner = FakePaths()
        runner.broker_source = '172.18.0.1'
        self.assertEqual(runner.identity()['status'],'inconclusive')

    def test_wrong_api_sequence_is_inconclusive(self):
        runner = FakePaths()
        runner.api_seq = 11
        self.assertEqual(runner.identity()['status'],'inconclusive')

    def test_restarted_publisher_during_collection_is_inconclusive(self):
        runner = FakePaths()
        runner.iot_pid_changes = True
        self.assertEqual(runner.identity()['status'],'inconclusive')

    def test_isolation_pass_requires_listeners_counters_and_cleanup(self):
        runner = FakePaths()
        report = runner.isolation()
        self.assertTrue(report['passed'])
        self.assertEqual(len(runner.cross_probes),2)
        self.assertEqual(len(report['cleanup']),2)
        self.assertTrue(all(row['verified'] for row in report['cleanup']))
        self.assertTrue(all(not row['active'] for row in runner.units.values()))
        self.assertTrue(all(row[2] in (paths.ADDRESSES['office'],paths.ADDRESSES['iot']) for row in runner.cross_probes))

    def test_wrong_inventory_never_starts_listener(self):
        runner = FakePaths()
        runner.bad_inventory = True
        self.assertEqual(runner.isolation()['status'],'inconclusive')
        self.assertEqual(runner.commands,[])

    def test_second_guard_failure_cleans_first_listener(self):
        runner = FakePaths()
        runner.guard_busy_role = 'iot'
        report = runner.isolation()
        self.assertEqual(report['status'],'inconclusive')
        self.assertEqual(len(report['cleanup']),1)
        self.assertFalse(runner.units['office']['active'])
        self.assertNotIn('iot',runner.units)

    def test_listener_start_ambiguous_timeout_is_cleaned(self):
        runner = FakePaths()
        runner.start_timeout_role = 'iot'
        report = runner.isolation()
        self.assertFalse(report['passed'])
        self.assertEqual(len(report['cleanup']),2)
        self.assertTrue(all(row['verified'] for row in report['cleanup']))
        self.assertNotIn('private exception',json.dumps(report))

    def test_counter_missing_is_inconclusive_even_when_port_rejected(self):
        runner = FakePaths()
        runner.no_counter = True
        report = runner.isolation()
        self.assertEqual(report['status'],'inconclusive')
        self.assertTrue(all(row['verified'] for row in report['cleanup']))

    def test_expired_target_cannot_prove_firewall_deny(self):
        runner = FakePaths()
        runner.expire_listener = True
        report = runner.isolation()
        self.assertEqual(report['status'],'inconclusive')
        denied = [row for row in report['checks'] if row['name'].endswith('9000_denied')]
        self.assertTrue(all(row['status']=='inconclusive' for row in denied))

    def test_unexpected_lateral_connection_fails(self):
        runner = FakePaths()
        runner.cross_allowed = True
        self.assertEqual(runner.isolation()['status'],'failed')

    def test_cleanup_failure_overrides_early_prerequisite_return(self):
        runner = FakePaths()
        runner.guard_busy_role = 'iot'
        runner.cleanup_failure_role = 'office'
        report = runner.isolation()
        self.assertEqual(report['status'],'failed')
        self.assertFalse(report['cleanup'][0]['verified'])
        self.assertIn('sudo systemctl stop warehouse-path-listener-',report['cleanup'][0]['manual_command'])

    def test_ehostunreachable_requires_counter_and_listener(self):
        row = {'source_ip':paths.ADDRESSES['office'],'connected':False,'errno':113}
        self.assertEqual(paths.classify_deny(row,paths.ADDRESSES['office'],True,{'verified':True}),'pass')
        self.assertEqual(paths.classify_deny(row,paths.ADDRESSES['office'],False,{'verified':True}),'inconclusive')
        self.assertEqual(paths.classify_deny(row,paths.ADDRESSES['office'],True,{'verified':False}),'inconclusive')

    def test_fixed_endpoint_and_owned_unit_guards(self):
        with self.assertRaises(ValueError):
            paths.socket_script('192.168.10.100','1.2.3.4')
        with self.assertRaises(ValueError):
            paths.listener_code('0.0.0.0')
        with self.assertRaises(ValueError):
            paths.unit_state_script('ssh.service','192.168.10.100',stop=True)

    def test_all_guest_scripts_compile_and_fit_serial_limit(self):
        unit = 'warehouse-path-listener-0123456789ab-office'
        scripts = [paths.iot_script(),paths.broker_script(),paths.office_script(),paths.unit_state_script(unit,'192.168.10.100'),paths.unit_state_script(unit,'192.168.10.100',stop=True),paths.socket_script('192.168.10.100','192.168.20.100')]
        for script in scripts:
            code = script + "\nimport json\nprint('\\nWH_PATH_"+'a'*32+"_'+json.dumps(r),flush=True)\n"
            compile(code,'<path-script-test>','exec')
            self.assertLess(len(paths.compressed_python(code).encode()),2900)
        self.assertNotIn('device.env',paths.iot_script())
        self.assertNotIn('firewall',paths.listener_code('192.168.10.100'))


if __name__ == '__main__':
    unittest.main()
