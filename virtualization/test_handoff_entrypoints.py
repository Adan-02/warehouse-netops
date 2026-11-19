"""Handoff contracts: help/import are inert; staging cannot touch a final lab."""
from contextlib import redirect_stdout
import importlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'virtualization'))
from virtualization.bootstrap import guards, stage_service_application
import cloud_connection
import lab_profile
import release


class ReadOnlyReleaseTests(unittest.TestCase):
    def test_status_never_resolves_or_creates_private_directory(self):
        relay = mock.MagicMock()
        with mock.patch.object(release, 'ServiceRelay', return_value=relay), \
                mock.patch.object(release, 'read_status', return_value={'current':None}) as read, \
                mock.patch.object(release, 'private_directory', side_effect=AssertionError('read-only status resolved private storage')), \
                mock.patch('builtins.print'):
            release.main(['status'])
        read.assert_called_once_with(relay.__enter__.return_value)


class StagingGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = SimpleNamespace(private=self.root/'private', vms=self.root/'vms')
        self.paths.private.mkdir()

    def guest(self, fields):
        directory = self.paths.vms/'service'
        directory.mkdir(parents=True)
        (directory/'service.vmx').write_text(fields, encoding='utf-8')

    def test_new_runtime_and_existing_staging_are_allowed_without_writes(self):
        guards.require_staging(self.paths)
        self.guest('ethernet0.connectionType = "custom"\nethernet0.vnet = "VMnet8"\n')
        before = list(self.root.rglob('*'))
        guards.require_staging(self.paths)
        self.assertEqual(list(self.root.rglob('*')), before)

    def test_release_receipt_blocks_before_connect_or_source_archive(self):
        (self.paths.private/'last-publish.json').write_text('{}')
        with mock.patch.object(guards, 'load_paths', return_value=self.paths), \
                mock.patch.object(stage_service_application, 'connect') as connect, \
                mock.patch.object(stage_service_application.tarfile, 'open') as archive, \
                self.assertRaisesRegex(RuntimeError, 'release deployment'):
            stage_service_application.main([])
        connect.assert_not_called()
        archive.assert_not_called()

    def test_isolated_or_unknown_guest_cannot_enter_staging(self):
        for fields in ('ethernet0.connectionType = "pvn"\n', 'ethernet0.connectionType = "nat"\n', ''):
            with self.subTest(fields=fields):
                if not (self.paths.vms/'service').exists(): self.guest(fields)
                else: (self.paths.vms/'service/service.vmx').write_text(fields)
                with self.assertRaisesRegex(RuntimeError, 'not a staging'):
                    guards.require_staging(self.paths)

    def test_remote_release_layout_is_checked_before_writing_the_archive(self):
        client = mock.Mock()
        with mock.patch.object(guards, 'require_staging'), \
                mock.patch.object(stage_service_application, 'connect', return_value=client), \
                mock.patch.object(stage_service_application, 'execute', side_effect=RuntimeError('existing release')) as execute, \
                mock.patch.object(stage_service_application.tarfile, 'open') as archive, \
                self.assertRaisesRegex(RuntimeError, 'existing release'):
            stage_service_application.main([])
        self.assertIn('/opt/warehouse-netops/current', execute.call_args.args[1])
        archive.assert_not_called()
        client.close.assert_called_once()


class ImportAndHelpTests(unittest.TestCase):
    def environment(self):
        # Any accidental load_profile() during import/help fails immediately.
        return dict(os.environ, WAREHOUSE_LAB_PROFILE=str(ROOT/'.local/does-not-exist-handoff-test.json'))

    def test_read_only_modules_do_not_load_personal_settings_when_imported(self):
        code = "import sys;sys.path.insert(0,'virtualization');import runtime,doctor,download_images,cloud_connection,control,collect_status;from virtualization.bootstrap import create_router,create_guests,create_guest_disks,install_router_packages,stage_service_application,provision_staging_guests,finalize_isolated_network"
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_operator_help_exits_before_config_or_runtime_activity(self):
        scripts = ('prepare_router', 'prepare_guests', 'prepare_disks', 'bootstrap_router',
                   'deploy_service', 'provision_guests', 'switch_to_isolated', 'control',
                   'collect_status', 'cloud_connection', 'inspect_local', 'diagnose_serial',
                   'diagnose_tunnel', 'update_publisher', 'release', 'verify_remote',
                   'apply_virtual_router', 'download_images', 'install_observers')
        for name in scripts:
            with self.subTest(script=name):
                result = subprocess.run([sys.executable, 'virtualization/'+name+'.py', '--help'], cwd=ROOT, env=self.environment(), capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout.lower())
                self.assertNotIn('starting', result.stdout.lower())

    def test_canonical_bootstrap_module_help_works(self):
        result = subprocess.run([sys.executable, '-m', 'virtualization.bootstrap.stage_service_application', '--help'], cwd=ROOT, env=self.environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout.lower())


class ManagementAliasTests(unittest.TestCase):
    def connect(self, hostname='10.77.0.1', user='qqops'):
        configuration = mock.Mock()
        configuration.lookup.return_value = {'hostname':hostname, 'user':user,
                                             'userknownhostsfile':'pinned-known-hosts', 'identityfile':['private-key']}
        client = mock.Mock()
        with mock.patch.object(cloud_connection.paramiko, 'SSHConfig', return_value=configuration), \
                mock.patch.object(cloud_connection.paramiko, 'SSHClient', return_value=client), \
                mock.patch.object(Path, 'open', mock.mock_open()), \
                mock.patch.object(Path, 'is_file', return_value=True):
            result = cloud_connection.connect_guangzhou({'cloud_ssh_alias':'renamed-owned-alias'})
        return configuration, client, result

    def test_profile_alias_is_consumed_with_original_pins(self):
        configuration, client, result = self.connect()
        configuration.lookup.assert_called_once_with('renamed-owned-alias')
        self.assertIs(result, client)
        self.assertEqual(client.connect.call_args.kwargs['username'], 'qqops')
        self.assertFalse(client.connect.call_args.kwargs['look_for_keys'])
        client.load_host_keys.assert_called_once_with('pinned-known-hosts')

    def test_alias_cannot_select_a_different_host_or_identity(self):
        for hostname, user in (('203.0.113.5','qqops'), ('10.77.0.1','root')):
            with self.subTest(hostname=hostname, user=user), self.assertRaisesRegex(RuntimeError, 'alias changed'):
                self.connect(hostname, user)


if __name__ == '__main__':
    unittest.main()
