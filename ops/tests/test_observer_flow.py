"""Observer orchestration contracts; all transport and notification I/O is fake."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ops import observer
from ops.common import iso_time
from ops.paths import PRIVATE_DIR, OBSERVATIONS_DIR, observer_paths
from ops.webhook import DeliveryStore, NotificationState

PEER = 'AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE='
NOW = 1000


def runner(arguments, **kwargs):
    if arguments[-1] == 'warehouse-report-v1':
        return '{"accepted":true}'
    return json.dumps({'peers': [PEER], 'handshakes': {PEER: 990}, 'service_route_device': 'wg-warehouse'})


def getter(path):
    if path == '/api/health':
        return {'connected': True, 'status': 200, 'data': {'state': 'healthy'}}
    return {'status': 200, 'data': {'changes': [], 'latest_change_id': 0, 'next_after_id': 0}}


class PathContractTests(unittest.TestCase):
    def test_templates_and_installation_default_to_same_private_directory(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('site.config.example.json', 'cloud.config.example.json'):
            paths = observer_paths(json.loads((root/name).read_text()))
            self.assertEqual(paths.private_dir, PRIVATE_DIR.as_posix())
            self.assertEqual(paths.observations_dir, OBSERVATIONS_DIR.as_posix())
        self.assertEqual(observer_paths({}).private_dir, PRIVATE_DIR.as_posix())

    def test_legacy_state_directory_keeps_existing_keys_and_outbox_location(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = {'state_dir': directory}
            canonical = {'private_dir': directory}
            self.assertEqual(observer_paths(legacy), observer_paths(canonical))
            self.assertEqual(observer.router_observation_ssh(legacy), observer.router_observation_ssh(canonical))
            self.assertEqual(observer.snapshot_report_ssh(legacy), observer.snapshot_report_ssh(canonical))
            for invalid in ('relative-dir', '/tmp/bad\npath', ''):
                with self.assertRaises(ValueError): observer_paths({'private_dir': invalid})
            with self.assertRaises(ValueError):
                observer_paths({'state_dir': directory, 'private_dir': '/different-location'})

    def test_ssh_capabilities_are_explicit_and_retain_fixed_privilege_boundary(self):
        site = observer.router_observation_ssh({})
        report = observer.snapshot_report_ssh({})
        self.assertEqual(site[-2:], ['root@192.168.30.1', 'warehouse-observe-v1'])
        self.assertEqual(report[-2:], ['warehouse@192.168.30.10', 'warehouse-report-v1'])
        self.assertEqual(site[site.index('-b')+1], '192.168.30.10')
        self.assertEqual(report[report.index('-b')+1], '10.89.0.1')
        for command in (site, report):
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertIn('PasswordAuthentication=no', command)
            self.assertIn('IdentityAgent=none', command)
        self.assertIs(DeliveryStore, NotificationState)
        self.assertIs(observer.cloud_once, observer.guangzhou_once)


class CycleDiagnosticsTests(unittest.TestCase):
    def test_failure_stages_persist_success_timestamps_and_do_not_block_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'expected_peer': PEER, 'private_dir': directory, 'webhook': {'enabled': False}}
            first = observer.guangzhou_once(config, runner=runner, getter=getter, clock=lambda: NOW)
            self.assertEqual(first['stages']['report']['state'], 'healthy')
            private_text = 'https://private.example/credential?token=DO_NOT_ECHO'
            def partial_runner(arguments, **kwargs):
                if arguments[-1] == 'warehouse-report-v1':
                    raise TimeoutError(private_text)
                return runner(arguments, **kwargs)
            def partial_getter(path):
                if path != '/api/health':
                    raise ValueError(private_text)
                return getter(path)
            with mock.patch.object(NotificationState, 'dispatch', side_effect=RuntimeError(private_text)):
                second = observer.guangzhou_once(config, runner=partial_runner, getter=partial_getter, clock=lambda: NOW+10)
            stages = second['stages']
            self.assertEqual(stages['report'], {'state': 'unhealthy', 'reason_code': 'report_transport_failed', 'last_success_at': iso_time(NOW)})
            self.assertEqual(stages['event_sync']['reason_code'], 'event_sync_failed')
            self.assertEqual(stages['dispatch']['reason_code'], 'dispatch_failed')
            self.assertEqual(stages['maintenance']['state'], 'healthy')
            saved = json.loads((Path(directory)/'status.json').read_text())
            self.assertEqual(saved['stages'], stages)
            self.assertNotIn(private_text, json.dumps(second))
            self.assertNotIn(private_text, json.dumps(saved))

    def test_notification_database_failure_does_not_prevent_snapshot_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'expected_peer': PEER, 'private_dir': directory}
            with mock.patch.object(observer, 'NotificationState', side_effect=sqlite3.OperationalError('sensitive database path')):
                result = observer.guangzhou_once(config, runner=runner, getter=getter, clock=lambda: NOW)
            self.assertTrue(result['report_delivered'])
            self.assertEqual(result['stages']['reachability']['state'], 'unhealthy')
            self.assertEqual(result['stages']['event_sync']['state'], 'unknown')
            self.assertNotIn('sensitive database path', json.dumps(result))

    def test_unreachable_service_is_distinct_from_disabled_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'expected_peer': PEER, 'state_dir': directory, 'webhook': {'enabled': False}}
            result = observer.cloud_once(config, runner=runner,
                getter=lambda path: {'connected': False, 'status': None, 'data': None}, clock=lambda: NOW)
            self.assertEqual(result['stages']['event_sync']['state'], 'skipped')
            self.assertEqual(result['stages']['dispatch']['state'], 'disabled')
            self.assertTrue(result['notifications']['remote_event_active'])

    def test_cloud_cli_alias_exposes_canonical_identity_and_stages(self):
        value = {'snapshot': {'components': {}}, 'report_delivered': False,
            'notifications': {'enabled': False}, 'stages': {'report': {'state': 'unhealthy'}}}
        output = io.StringIO()
        with mock.patch('sys.argv', ['observer', 'cloud', '--config', 'unused']), \
                mock.patch.object(observer, 'private_config', return_value={}), \
                mock.patch.object(observer, 'guangzhou_once', return_value=value), redirect_stdout(output):
            self.assertEqual(observer.main(), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary['role'], 'cloud')
        self.assertEqual(summary['observer_id'], 'guangzhou')
        self.assertEqual(summary['stages'], value['stages'])


if __name__ == '__main__':
    unittest.main()
