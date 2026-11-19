"""Read-only layering, explicit lock lifetime and one-shot status regressions."""
import ast
import importlib.util
import json
import os
import random
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import lab_session
if os.name=='nt':
    import console
else:
    native={name:types.ModuleType(name) for name in ('win32file','win32pipe','win32event','pywintypes')}
    with mock.patch.dict(sys.modules,native):import console
import release
import probes
from transport_limits import command_limit


class SessionBoundaryTests(unittest.TestCase):
    def test_probe_limit_matches_shell_before_opening_a_console(self):
        factory=mock.Mock()
        randomizer=random.Random(0)
        incompressible=''.join(randomizer.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(5000))
        with lab_session.LabSession(factory) as session,\
                mock.patch.object(lab_session,'guest_script',return_value=incompressible):
            with self.assertRaises(probes.ProtocolError):session.guest('office','inventory')
        factory.assert_not_called()
        with self.assertRaises(probes.ProtocolError):probes.encoded_python(incompressible,role='router')
        self.assertEqual(command_limit('router'),380)
        self.assertEqual(command_limit('office'),3000)

    def test_readonly_layers_do_not_import_mutating_scenarios(self):
        folder=Path(__file__).resolve().parent
        forbidden={'check_matrix','check_faults','check_paths','provision_guests','deploy_service'}
        for name in ('probes','lab_session','live_health'):
            tree=ast.parse((folder/(name+'.py')).read_text(encoding='utf-8'))
            modules=[node.module for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
            self.assertFalse(forbidden.intersection(modules),name)

    def test_imports_with_an_invalid_personal_profile_do_not_initialize_it(self):
        folder=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            environment=dict(os.environ,WAREHOUSE_LAB_PROFILE=str(Path(directory)/'missing-profile.json'))
            program='''import sys,types
sys.path.insert(0,sys.argv[1])
if sys.platform!='win32':
 sys.modules.update({name:types.ModuleType(name) for name in ('win32file','win32pipe','win32event','pywintypes')})
import live_health,check_matrix,check_faults,check_paths,check_wan,verify_remote,release
print('imported-without-profile')
'''
            result=subprocess.run([sys.executable,'-c',program,str(folder)],env=environment,
                capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'imported-without-profile')

    def test_one_operation_lock_covers_all_connections_and_exception_cleanup(self):
        events=[]
        class Lock:
            def acquire(self):events.append('lock')
            def close(self):events.append('unlock')
        class RawShell:
            def __init__(self,role):self.role=role;events.append('open '+role)
            def close(self):events.append('close '+self.role)
        class CompatibilityFactory:
            uses_lab_operation_lock=True
        with mock.patch.object(console,'LabOperationLock',Lock),mock.patch.object(console,'VmSerialShell',RawShell):
            with self.assertRaisesRegex(RuntimeError,'fake fault'):
                with lab_session.LabSession(CompatibilityFactory) as session:
                    self.assertIs(session.console('office'),session.console('office'))
                    session.console('iot')
                    self.assertEqual(events,['lock','open office','open iot'])
                    raise RuntimeError('fake fault')
        self.assertEqual(events,['lock','open office','open iot','close office','close iot','unlock'])

    def test_standalone_compatibility_console_keeps_lock_on_open_failure(self):
        events=[]
        class Lock:
            def acquire(self):events.append('lock')
            def close(self):events.append('unlock')
        with mock.patch.object(console,'LabOperationLock',Lock),\
                mock.patch.object(console.win32file,'CreateFile',side_effect=OSError('fake pipe unavailable'),create=True):
            with self.assertRaises(OSError):console.Console('office')
        self.assertEqual(events,['lock','unlock'])


class ReadonlyReleaseProbeTests(unittest.TestCase):
    def load_probe(self):
        path=Path(__file__).with_name('remote_probes')/'release_status.py'
        spec=importlib.util.spec_from_file_location('readonly_release_probe',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_status_uses_small_readonly_program_not_release_manager(self):
        relay=mock.Mock();relay.run.return_value=(0,'{"current":"release","image_id":"image"}')
        with mock.patch.object(release,'install_manager') as install:
            release.read_status(relay)
        install.assert_not_called()
        import shlex
        source=shlex.split(relay.run.call_args.args[0])[2]
        tree=ast.parse(source)
        functions={node.name for node in tree.body if isinstance(node,ast.FunctionDef)}
        self.assertEqual(functions,{'run','docker','current_path','http','status'})
        self.assertNotIn('activate',functions)
        self.assertNotIn('transaction',functions)

    @unittest.skipIf(os.name=='nt','Symlink behavior is exercised on Linux')
    def test_managed_release_inventory_never_creates_or_installs_state(self):
        probe=self.load_probe()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'releases/v1.0.0-0123456789ab'
            target.mkdir(parents=True);(root/'private').mkdir()
            (target/'manifest.json').write_text(json.dumps({'release_id':target.name}))
            (root/'current').symlink_to(target,target_is_directory=True)
            before=set(root.rglob('*'))
            with mock.patch.object(probe,'ROOT',root),\
                    mock.patch.object(probe,'docker',return_value=mock.Mock(stdout='sha256:image\n')) as docker,\
                    mock.patch.object(probe,'http',side_effect=[{'data_status':'online'},{'version':'v1.0.0'}]),\
                    mock.patch.object(Path,'mkdir',side_effect=AssertionError('status must not create directories')),\
                    mock.patch.object(Path,'write_text',side_effect=AssertionError('status must not write files')):
                value=probe.status()
            self.assertEqual(value['current'],target.name)
            self.assertEqual(value['image_id'],'sha256:image')
            self.assertEqual(value['business']['data_status'],'online')
            self.assertEqual(set(root.rglob('*')),before)
            docker.assert_called_once_with('inspect',probe.CONTAINER,'--format','{{.Image}}')


if __name__=='__main__':unittest.main()
