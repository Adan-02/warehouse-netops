"""Offline regression checks for identity, path and cleanup evidence guards."""
import ast
import base64
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import verify_remote as remote

PEER = base64.b64encode(bytes(range(32))).decode('ascii')


def good_path():
    return {'route_exit':0,'peers_exit':0,'allowed_exit':0,'handshakes_exit':0,
            'routes':[{'dev':'wg-warehouse','from':'10.89.0.1'}], 'peers':[PEER],
            'allowed':[{'peer':PEER,'ips':['192.168.30.10/32','10.89.0.2/32']}],
            'handshakes':[{'peer':PEER,'epoch':1000}], 'observed_at':1001}


def good_http():
    return {'source_ip':'10.89.0.1','connected':True,'status':200,
            'data':{'device_id':'cabinet-01','broker_connected':True,'data_status':'online',
                    'demo_mode':True,'seq':10,'offline_after_seconds':20}}


class RemoteGuardTests(unittest.TestCase):
    def test_ssh_identity_is_exact(self):
        self.assertTrue(remote.ssh_identity_matches(0,'1000\nwarehouse\nwarehouse-service\n'))
        for code, output in [(0,'0\nroot\nwarehouse-service\n'), (0,'root\nwarehouse-service\n'),
                             (0,'1000\nwarehouse\nwarehouse-service-spoof\n'),
                             (1,'1000\nwarehouse\nwarehouse-service\n')]:
            self.assertFalse(remote.ssh_identity_matches(code,output))

    def test_path_requires_exact_interface_peer_and_allowed_ips(self):
        self.assertTrue(remote.path_ready(good_path(),PEER))
        mutations = [lambda v:v['routes'][0].update(dev='wg0'),
                     lambda v:v['routes'][0].update({'from':'10.77.0.1'}),
                     lambda v:v['peers'].append('unrelated'),
                     lambda v:v['allowed'][0]['ips'].append('0.0.0.0/0'),
                     lambda v:v['allowed'][0].update(peer='unrelated'),
                     lambda v:v['handshakes'][0].update(peer='unrelated'),
                     lambda v:v.update(handshakes_exit=1)]
        for change in mutations:
            with self.subTest(change=change):
                value=good_path();change(value)
                self.assertFalse(remote.path_ready(value,PEER))

    def test_handshake_requires_successful_command_and_expected_peer(self):
        value=good_path()
        self.assertTrue(remote.recent_handshake(remote.expected_handshake(value,PEER)))
        value['handshakes_exit']=1
        self.assertFalse(remote.recent_handshake(remote.expected_handshake(value,PEER)))
        value=good_path();value['handshakes'][0]['peer']='other'
        self.assertFalse(remote.recent_handshake(remote.expected_handshake(value,PEER)))

    def test_private_stat_guard_rejects_symlink_permissions_and_owner(self):
        # Exercise the exact generated guard function with synthetic lstat data.
        tree=ast.parse(remote.private_guard_source('expected-owner'))
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='guarded')
        namespace={'stat':stat}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<private-guard>','exec'),namespace)
        guard=namespace['guarded']
        good=SimpleNamespace(lstat=lambda:SimpleNamespace(st_uid=0,st_mode=stat.S_IFREG|0o600))
        guard(good)
        for uid, mode in [(1000,stat.S_IFREG|0o600),(0,stat.S_IFREG|0o644),
                          (0,stat.S_IFLNK|0o600),(0,stat.S_IFDIR|0o600)]:
            with self.assertRaises(RuntimeError):
                guard(SimpleNamespace(lstat=lambda:SimpleNamespace(st_uid=uid,st_mode=mode)))

    def test_generated_remote_scripts_compile(self):
        for text in (remote.maintenance_key_script('expected-owner'),
                     remote.host_pin_script('expected-owner','192.168.30.10 ssh-ed25519 TEST\n'),
                     remote.path_script()):
            compile(text,'<remote-script>','exec')

    def test_wrong_bundle_owner_is_rejected_before_key_access(self):
        class FakePath:
            def __init__(self,name):self.name=name
            def __truediv__(self,child):return FakePath(self.name+'/'+child)
            def lstat(self):
                directory=not self.name.endswith('/.owner')
                return SimpleNamespace(st_uid=0,st_mode=(stat.S_IFDIR|0o700) if directory else (stat.S_IFREG|0o600))
            def read_text(self):return 'different-owner\n'
        with mock.patch('pathlib.Path',FakePath),mock.patch('os.umask') as mask:
            with self.assertRaisesRegex(RuntimeError,'ownership marker differs'):
                exec(remote.private_guard_source('expected-owner'),{})
            mask.assert_not_called()

    def test_existing_keypair_mismatch_and_symlink_are_rejected(self):
        class FakePath:
            def __init__(self,name,symlink=False):self.name=name;self.symlink=symlink
            def __truediv__(self,child):return FakePath(child,self.symlink and child=='maintenance_ed25519')
            def is_symlink(self):return self.symlink
            def exists(self):return True
            def read_text(self):return 'ssh-ed25519 DIFFERENT comment\n'
            def __str__(self):return self.name
        with mock.patch.object(remote,'private_guard_source',return_value=''):
            script=remote.maintenance_key_script('expected-owner')
        subprocess=mock.Mock();subprocess.check_output.return_value='ssh-ed25519 ACTUAL comment\n'
        with self.assertRaisesRegex(RuntimeError,'private/public keys differ'):
            exec(script,{'private':FakePath('private'),'guarded':mock.Mock(),'subprocess':subprocess})
        subprocess.run.assert_not_called()
        subprocess.reset_mock()
        with self.assertRaisesRegex(RuntimeError,'symlinks'):
            exec(script,{'private':FakePath('private',True),'guarded':mock.Mock(),'subprocess':subprocess})
        subprocess.check_output.assert_not_called()

    def test_ssh_preparation_keeps_serial_payload_below_transport_limit(self):
        service=mock.Mock()
        service.command.side_effect=[{'exit_code':0,'output':'ssh-ed25519 AAAA service'},
                                     {'exit_code':0,'output':'public key present'}]
        with mock.patch.object(remote,'checked',side_effect=['ssh-ed25519 AAAA','host pin verified']):
            remote.prepare_ssh(object(),service,'expected-owner')
        self.assertTrue(all(len(call.args[0].encode())<=3000 for call in service.command.call_args_list))


class MainCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project=Path(self.temporary.name)
        self.private=self.project/'.secrets/virtual-lab'
        self.private.mkdir(parents=True)
        (self.project/'evidence').mkdir()
        self.baseline={'protected':'unchanged'}
        (self.private/'guangzhou-before.json').write_text(json.dumps(self.baseline))
        self.client=mock.Mock()
        self.runner=mock.Mock()
        self.runner.guest.return_value={
            'nics':[{'name':'eth0','addresses':['192.168.30.10/24'],'up':True}],
            'routes':[{'dst':'default','gateway':'192.168.30.1','dev':'eth0'}],
            'self_connect':{'22':True,'1883':True,'8080':True}}
        self.runner.router.return_value={'fw4_check_exit':0,'rules':[]}
        self.ports={'ports':{str(port):{'source_ip':'10.89.0.1','connected':port!=1883} for port in (22,1883,8080)}}
        patches=[mock.patch.object(remote,'PROJECT',self.project),mock.patch.object(remote,'PRIVATE',self.private),
                 mock.patch.object(remote,'expectations',return_value=('expected-owner',PEER)),
                 mock.patch.object(remote,'connect_guangzhou',return_value=self.client),
                 mock.patch.object(remote,'MatrixRunner',return_value=self.runner),
                 mock.patch.object(remote,'audit',return_value=self.baseline),
                 mock.patch.object(remote,'deny_counter_evidence',return_value={'verified':True}),
                 mock.patch.object(remote,'prepare_ssh'),
                 mock.patch.object(remote,'exec_collect',return_value=(0,'1000\nwarehouse\nwarehouse-service\n'))]
        self.mocks={}
        for patcher in patches:
            result=patcher.start();self.addCleanup(patcher.stop)
            self.mocks[patcher.attribute]=result

    def command_outputs(self,http=None,path=None):
        return ['\nWH_REMOTE_PORTS_'+json.dumps(self.ports),
                '\nWH_REMOTE_HTTP_'+json.dumps(http or good_http()),
                '\nWH_REMOTE_PATH_'+json.dumps(path or good_path())]

    def run_main(self,outputs=None):
        with mock.patch.object(remote,'checked',side_effect=outputs or self.command_outputs()),redirect_stdout(io.StringIO()) as output:
            code=remote.main()
        return code,json.loads(output.getvalue())

    def test_verified_success_closes_both_transports(self):
        code,summary=self.run_main()
        self.assertEqual(code,0);self.assertTrue(summary['passed'])
        self.runner.close.assert_called_once();self.client.close.assert_called_once()
        self.mocks['prepare_ssh'].assert_called_once()

    def test_old_substring_root_identity_cannot_pass(self):
        self.mocks['exec_collect'].return_value=(0,'root\nwarehouse-service\n')
        code,summary=self.run_main()
        self.assertEqual(code,1);self.assertFalse(summary['passed'])

    def test_fw4_failure_blocks_ssh_preparation(self):
        self.runner.router.return_value={'fw4_check_exit':1,'rules':[]}
        code,summary=self.run_main()
        self.assertEqual(code,1);self.assertFalse(summary['passed'])
        self.mocks['prepare_ssh'].assert_not_called()

    def test_wrong_device_online_is_not_fresh_expected_device(self):
        http=good_http();http['data']['device_id']='different-device'
        code,summary=self.run_main(self.command_outputs(http=http))
        self.assertEqual(code,1);self.assertFalse(summary['passed'])
        self.mocks['prepare_ssh'].assert_not_called()

    def test_failed_probe_and_failed_audit_still_close(self):
        self.mocks['audit'].side_effect=[self.baseline,RuntimeError('audit failed')]
        code,summary=self.run_main([RuntimeError('probe failed')])
        self.assertEqual(code,1);self.assertFalse(summary['passed'])
        self.runner.close.assert_called_once();self.client.close.assert_called_once()

    def test_evidence_write_failure_returns_failure_after_closing(self):
        original_write=Path.write_text
        def write(path,*args,**kwargs):
            if path.name=='virtual-remote.json':raise OSError('disk full')
            return original_write(path,*args,**kwargs)
        with mock.patch.object(Path,'write_text',write):
            code,summary=self.run_main()
        self.assertEqual(code,1);self.assertFalse(summary['passed'])
        self.assertIn({'name':'evidence_written','status':'failed'},summary['checks'])
        self.runner.close.assert_called_once();self.client.close.assert_called_once()

    def test_runner_close_failure_does_not_skip_client_close(self):
        self.runner.close.side_effect=OSError('console failure')
        code,summary=self.run_main()
        self.assertEqual(code,1);self.assertFalse(summary['passed'])
        self.client.close.assert_called_once()

    def test_connection_failure_is_reported_without_opening_console(self):
        self.mocks['connect_guangzhou'].side_effect=OSError('no route')
        code,summary=self.run_main()
        self.assertEqual(code,1);self.assertFalse(summary['passed'])
        self.mocks['MatrixRunner'].assert_not_called()

    def test_missing_baseline_prevents_network_access(self):
        (self.private/'guangzhou-before.json').unlink()
        code,summary=self.run_main()
        self.assertEqual(code,1);self.assertFalse(summary['passed'])
        self.mocks['connect_guangzhou'].assert_not_called()


if __name__=='__main__':
    unittest.main()
