"""Offline release, database snapshot and final-network transport guard tests."""
import importlib
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import release
import release_guest as guest
import release_transport as transport


def docker_export():
    config=b'{"architecture":"amd64","os":"linux","config":{}}'
    identity=hashlib.sha256(config).hexdigest()
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w') as archive:
        for name,data in ((identity+'.json',config),('manifest.json',json.dumps([{'Config':identity+'.json','RepoTags':['warehouse:test'],'Layers':[]}]).encode())):
            member=tarfile.TarInfo(name);member.size=len(data);archive.addfile(member,io.BytesIO(data))
    return 'sha256:'+identity,output.getvalue()


class ArtifactTests(unittest.TestCase):
    def test_status_runs_in_memory_without_installing_or_creating_state(self):
        relay=mock.Mock();relay.run.return_value=(0,'{"current":"legacy-unmanaged","image_id":"image"}')
        with mock.patch.object(release,'install_manager') as install:
            value=release.read_status(relay)
        self.assertEqual(value['current'],'legacy-unmanaged');install.assert_not_called()
        command=relay.run.call_args.args[0]
        self.assertTrue(command.startswith('python3 -c '))
        self.assertNotIn('mkdir',command);self.assertNotIn('cat >',command)

    def test_manager_upload_fails_closed_before_directory_or_file_writes(self):
        source=(release.PROJECT/'virtualization/release_guest.py').read_bytes()
        with mock.patch.object(release,'checked',side_effect=['',hashlib.sha256(source).hexdigest()]) as checked:
            release.install_manager(mock.Mock())
        command=checked.call_args_list[0].args[1]
        self.assertTrue(command.startswith('set -eu;'))
        self.assertLess(command.index('test ! -L'),command.index('mkdir'))

    def test_remote_bundle_upload_attempts_are_unique_and_refuse_clobber(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'server/app').mkdir(parents=True)
            (root/'server/app/main.py').write_text('source')
            artifact=root/'artifacts';artifact.mkdir();(artifact/'bundle.tar').write_bytes(b'bundle')
            manifest={'release_id':'v1.0.0-0123456789ab','version':'v1.0.0','git_commit':'commit','bundle_sha256':'0'*64}
            relay=mock.Mock();relay.cloud_run.return_value=(0,json.dumps({'version':'v1.0.0','git_commit':'commit'}))
            def manager(relay,action,*args,**kwargs):return {'app_files':{}} if action=='baseline' else {'ready':True}
            with mock.patch.object(release,'PROJECT',root),mock.patch.object(release,'PRIVATE',root),mock.patch.object(release,'resolve_artifact',return_value=(artifact,manifest)),mock.patch.object(release,'install_manager'),mock.patch.object(release,'manager',side_effect=manager) as managed,mock.patch.object(release,'preserve_legacy_image',return_value={}),mock.patch.object(release,'backup',return_value={}),mock.patch.object(release,'checked',return_value='') as checked:
                release.deploy(relay,manifest['release_id']);release.deploy(relay,manifest['release_id'])
            commands=[row.args[1] for row in checked.call_args_list]
            self.assertEqual(len(commands),2);self.assertNotEqual(commands[0],commands[1])
            for command in commands:
                self.assertTrue(command.startswith('set -eu;'));self.assertIn('set -C; cat >',command)
            received=[row for row in managed.call_args_list if row.args[1]=='receive']
            self.assertEqual(len(received),2)
            for row in received:
                args=row.args;index=args.index('--upload-id');self.assertRegex(args[index+1],r'^[0-9a-f]{32}$')

    def test_upload_attempt_identifier_cannot_escape_incoming_directory(self):
        with self.assertRaises(ValueError):guest.receive('v1.0.0-0123456789ab','0'*64,'../outside')

    def test_release_requires_clean_revision(self):
        with mock.patch.object(release,'command',return_value=mock.Mock(stdout=b' M server/app/main.py')):
            with self.assertRaises(RuntimeError):release.clean_revision()

    def test_invalid_version_does_not_run_build(self):
        with mock.patch.object(release,'clean_revision') as revision:
            for value in ('../v1','v1.0;reboot','v1.0.0/else','1.0.0'):
                with self.subTest(value=value),self.assertRaises(ValueError):release.build(value)
            revision.assert_not_called()

    def test_unknown_artifact_cannot_escape_release_directory(self):
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(release,'ARTIFACTS',Path(temp)):
            with self.assertRaises(ValueError):release.resolve_artifact('../elsewhere')

    def test_changed_outer_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(release,'ARTIFACTS',Path(temp)):
            folder=Path(temp)/'v1.0.0-0123456789ab';folder.mkdir()
            (folder/'bundle.tar').write_bytes(b'changed')
            (folder/'build.json').write_text(json.dumps({'release_id':folder.name,'bundle_sha256':'0'*64}))
            with self.assertRaises(RuntimeError):release.resolve_artifact(folder.name)

    def test_unsafe_archive_names_and_links_are_rejected(self):
        for name,kind in (('../escape',tarfile.REGTYPE),('/root/file',tarfile.REGTYPE),('server/link',tarfile.SYMTYPE),('other/file',tarfile.REGTYPE)):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as temp:
                folder=Path(temp);archive=folder/'source.tar'
                with tarfile.open(archive,'w') as out:
                    member=tarfile.TarInfo(name);member.type=kind;member.size=0
                    if kind==tarfile.SYMTYPE:member.linkname='/etc'
                    out.addfile(member)
                with self.assertRaises(RuntimeError):guest.extract_checked(archive,folder/'out')

    def test_restore_checks_copy_and_do_not_modify_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'backup.sqlite3'
            conn=sqlite3.connect(path)
            conn.execute('CREATE TABLE readings (id INTEGER PRIMARY KEY,seq INTEGER,received_at REAL)')
            conn.execute('INSERT INTO readings(seq,received_at) VALUES(7,1000)');conn.commit();conn.close()
            expected={'sha256':release.sha256(path),'rows':1,'last':[7,1000.0]}
            path.with_suffix('.json').write_text(json.dumps(expected))
            original=path.read_bytes()
            result=release.restore_check(path)
            self.assertTrue(result['passed']);self.assertTrue(result['isolated_restore'])
            self.assertFalse(result['active_database_modified']);self.assertEqual(path.read_bytes(),original)

    def test_restore_checksum_failure_stops_before_opening_sqlite(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'backup.sqlite3';path.write_bytes(b'not a database')
            path.with_suffix('.json').write_text(json.dumps({'sha256':'0'*64}))
            with mock.patch.object(release.sqlite3,'connect') as connect,self.assertRaises(RuntimeError):release.restore_check(path)
            connect.assert_not_called()

    def test_private_backup_path_is_restricted(self):
        with self.assertRaises(RuntimeError):release.copy_backup(mock.Mock(),{'path':'/etc/shadow','sha256':'0'*64})

    def test_interrupted_backup_leaves_only_partial_and_retry_uses_new_attempt(self):
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(release,'PRIVATE',Path(temp)):
            data=b'complete immutable backup'
            snapshot={'path':release.REMOTE+'/private/backups/20261119T120000-01234567.sqlite3','sha256':hashlib.sha256(data).hexdigest()}
            attempts=[]
            def interrupted(relay,command,**kwargs):
                path=kwargs['output_file'];attempts.append(path);path.write_bytes(b'incomplete');raise TimeoutError('interrupted')
            with mock.patch.object(release,'checked',side_effect=interrupted),self.assertRaises(TimeoutError):release.copy_backup(mock.Mock(),snapshot)
            target=Path(temp)/'backups'/Path(snapshot['path']).name
            self.assertFalse(target.exists());self.assertTrue(attempts[0].exists())
            def complete(relay,command,**kwargs):
                attempts.append(kwargs['output_file']);kwargs['output_file'].write_bytes(data);return ''
            with mock.patch.object(release,'checked',side_effect=complete):
                self.assertEqual(release.copy_backup(mock.Mock(),snapshot),str(target))
            self.assertNotEqual(attempts[0],attempts[1]);self.assertEqual(target.read_bytes(),data)
            self.assertEqual(attempts[0].read_bytes(),b'incomplete');self.assertFalse(attempts[1].exists())
            self.assertEqual(json.loads(target.with_suffix('.json').read_text()),snapshot)

    def test_complete_backup_without_manifest_is_recovered_without_redownload(self):
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(release,'PRIVATE',Path(temp)):
            folder=Path(temp)/'backups';folder.mkdir();target=folder/'20261119T120000-01234567.sqlite3';target.write_bytes(b'complete')
            snapshot={'path':release.REMOTE+'/private/backups/'+target.name,'sha256':release.sha256(target)}
            with mock.patch.object(release,'checked') as checked:
                self.assertEqual(release.copy_backup(mock.Mock(),snapshot),str(target));checked.assert_not_called()
            self.assertEqual(json.loads(target.with_suffix('.json').read_text()),snapshot)

    def test_bad_download_hash_never_publishes_final_name(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'backup';relay=mock.Mock()
            def write(relay,cmd,**kwargs):kwargs['output_file'].write_bytes(b'corrupt')
            with mock.patch.object(release,'checked',side_effect=write),self.assertRaises(RuntimeError):
                release.download_private(relay,'/owned/backup',target,'0'*64)
            self.assertFalse(target.exists());self.assertEqual(len(list(Path(temp).glob('*.part'))),1)

    def test_atomic_publish_does_not_overwrite_a_racing_target(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'artifact';target.write_bytes(b'other')
            part=release.attempt_path(target);part.write_bytes(b'new')
            with self.assertRaises(RuntimeError):release.publish_private(part,target)
            self.assertEqual(target.read_bytes(),b'other');self.assertTrue(part.exists())

    def test_partial_legacy_download_is_preserved_and_retry_publishes_verified_image(self):
        image,tar=docker_export();packed=gzip.compress(tar)
        legacy={'image_id':image,'image_tag':'warehouse:legacy'}
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(release,'PRIVATE',Path(temp)),mock.patch.object(release.subprocess,'run',return_value=mock.Mock(returncode=1)):
            attempts=[]
            def interrupted(relay,cmd,**kwargs):
                attempts.append(kwargs['output_file']);attempts[-1].write_bytes(packed[:30]);raise TimeoutError('interrupted')
            with mock.patch.object(release,'checked',side_effect=interrupted),self.assertRaises(TimeoutError):release.preserve_legacy_image(mock.Mock(),legacy)
            directory=Path(temp)/'legacy-images';target=directory/(image.split(':')[1]+'.tar.gz')
            self.assertFalse(target.exists())
            def complete(relay,cmd,**kwargs):attempts.append(kwargs['output_file']);attempts[-1].write_bytes(packed)
            with mock.patch.object(release,'checked',side_effect=complete):value=release.preserve_legacy_image(mock.Mock(),legacy)
            self.assertEqual(value['sha256'],release.sha256(target));self.assertNotEqual(attempts[0],attempts[1]);self.assertTrue(attempts[0].exists())
            self.assertEqual(target.read_bytes(),packed)

    def test_legacy_image_without_manifest_recovers_only_with_matching_config_digest(self):
        image,tar=docker_export()
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(release,'PRIVATE',Path(temp)):
            directory=Path(temp)/'legacy-images';directory.mkdir()
            target=directory/(image.split(':')[1]+'.tar.gz');target.write_bytes(gzip.compress(tar))
            with mock.patch.object(release,'checked') as checked,mock.patch.object(release.subprocess,'run') as command:
                value=release.preserve_legacy_image(mock.Mock(),{'image_id':image,'image_tag':'warehouse:legacy'})
                checked.assert_not_called();command.assert_not_called()
            self.assertEqual(value['image_id'],image);self.assertTrue(target.with_suffix('.json').is_file())
            with self.assertRaises(RuntimeError):release.validate_image_export(target,'sha256:'+'0'*64)

    def test_local_docker_export_failure_keeps_part_and_can_retry(self):
        image,tar=docker_export()
        def process(code):
            proc=mock.Mock();proc.stdout=io.BytesIO(tar);proc.stderr=io.BytesIO();proc.wait.return_value=code;return proc
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'image.tar.gz'
            with mock.patch.object(release.subprocess,'Popen',return_value=process(1)),self.assertRaises(RuntimeError):release.save_image(image,target)
            self.assertFalse(target.exists());self.assertEqual(len(list(Path(temp).glob('*.part'))),1)
            with mock.patch.object(release.subprocess,'Popen',return_value=process(0)):release.save_image(image,target)
            release.validate_image_export(target,image)
            self.assertTrue(target.exists());self.assertEqual(len(list(Path(temp).glob('*.part'))),1)


class BaselineCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        patch=mock.patch.object(guest,'ROOT',self.root);patch.start();self.addCleanup(patch.stop)
        for name in ('releases','private','server'): (self.root/name).mkdir()
        (self.root/'server/compose.yaml').write_text('services:\n  dashboard:\n    build: .\n')
        (self.root/'server/.env').write_text('private values stay excluded')
        self.image='sha256:'+'1'*64
        self.docker=mock.patch.object(guest,'docker',side_effect=self.response);self.docker.start();self.addCleanup(self.docker.stop)

    def response(self,*args,**kwargs):
        if args[0]=='inspect':return mock.Mock(stdout=json.dumps([{'Image':self.image,'Mounts':[{'Name':guest.VOLUME,'Destination':'/data','Type':'volume'}]}]))
        if args[0]=='exec':return mock.Mock(stdout='{"main.py":"source-digest"}')
        return mock.Mock(stdout='')

    def test_invalid_compose_preflight_leaves_no_legacy_tree_and_retry_is_possible(self):
        compose=self.root/'server/compose.yaml';compose.write_text('unrecognized')
        with self.assertRaises(RuntimeError):guest.baseline()
        self.assertEqual(list((self.root/'releases').iterdir()),[])
        compose.write_text('services:\n  dashboard:\n    build: .\n')
        result=guest.baseline()
        self.assertEqual(result['image_id'],self.image);self.assertFalse((self.root/'releases/legacy/server/.env').exists())

    def test_copy_failure_cleans_only_own_staging_and_retry_succeeds(self):
        with mock.patch.object(guest.shutil,'copytree',side_effect=OSError('copy interrupted')),self.assertRaises(OSError):guest.baseline()
        self.assertEqual(list((self.root/'releases').iterdir()),[])
        self.assertEqual(guest.baseline()['release_id'],'legacy')

    def test_complete_legacy_tree_without_second_marker_can_finish_capture(self):
        original=guest.atomic_json
        def fail_marker(path,value):
            if path.name=='legacy-baseline.json':raise OSError('process interrupted before marker')
            return original(path,value)
        with mock.patch.object(guest,'atomic_json',side_effect=fail_marker),self.assertRaises(OSError):guest.baseline()
        self.assertTrue((self.root/'releases/legacy/manifest.json').exists());self.assertFalse((self.root/'private/legacy-baseline.json').exists())
        with mock.patch.object(guest,'docker') as docker:
            result=guest.baseline();docker.assert_not_called()
        self.assertEqual(json.loads((self.root/'private/legacy-baseline.json').read_text()),result)

    def test_partial_or_changed_existing_legacy_is_never_deleted(self):
        folder=self.root/'releases/legacy';folder.mkdir();file=folder/'operator-file';file.write_text('preserve')
        with self.assertRaises(RuntimeError):guest.baseline()
        self.assertEqual(file.read_text(),'preserve')


class ReadinessTests(unittest.TestCase):
    def check(self,legacy=False,storage='healthy'):
        moment=[0]
        def sleep(seconds):moment[0]+=seconds
        def http(path):
            if path=='/api/version':return {'version':'v1.0.0','git_commit':'commit','source_sha256':'source'}
            value={'device_id':'cabinet-01','broker_connected':True,'data_status':'online','last_seen_at':str(moment[0])}
            if storage is not None:value['storage_state']=storage
            return value
        manifest={'legacy':legacy,'image_id':'image','version':'v1.0.0','git_commit':'commit','source_sha256':'source'}
        with mock.patch.object(guest,'http',side_effect=http),mock.patch.object(guest,'docker',return_value=mock.Mock(stdout='image\n')),mock.patch.object(guest.time,'monotonic',side_effect=lambda:moment[0]),mock.patch.object(guest.time,'sleep',side_effect=sleep):
            return guest.wait_ready(manifest,timeout=5)

    def test_new_release_cannot_pass_with_unhealthy_or_absent_storage(self):
        for storage in ('unhealthy',None):
            with self.subTest(storage=storage),self.assertRaises(RuntimeError):self.check(storage=storage)

    def test_new_healthy_and_legacy_without_storage_field_remain_compatible(self):
        self.assertTrue(self.check()['sampling_progressed'])
        self.assertTrue(self.check(legacy=True,storage=None)['sampling_progressed'])


class ReleaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.patch=mock.patch.object(guest,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.runner=mock.patch.object(guest,'run',return_value=mock.Mock(stdout=''));self.runner.start();self.addCleanup(self.runner.stop)
        (self.root/'releases').mkdir();(self.root/'shared').mkdir();(self.root/'private').mkdir()
        self.old=self.root/'releases/legacy';self.old.mkdir()
        self.new=self.root/'releases/v1.0.0-0123456789ab';self.new.mkdir()
        for path,tag in ((self.old,'legacy'),(self.new,'new')):
            (path/'manifest.json').write_text(json.dumps({'release_id':path.name,'image_tag':tag,'image_id':'sha256:'+tag,'schema_min':1,'schema_max':3,'legacy':tag=='legacy'}))
        (self.new/'prepared.json').write_text('{}')
        (self.root/'shared/server.env').write_text('DASHBOARD_IMAGE=legacy\n')
        (self.root/'shared/config.json').write_text('{}')

    def test_incompatible_schema_never_stops_or_recreates_service(self):
        with mock.patch.object(guest,'bootstrap_layout'),mock.patch.object(guest,'current_path',return_value=self.old),mock.patch.object(guest,'schema_version',return_value={'schema_version':4}),mock.patch.object(guest,'compose') as compose:
            with self.assertRaises(RuntimeError):guest.activate(self.new.name)
            compose.assert_not_called()

    def test_failed_activation_restarts_old_image_and_retains_db(self):
        with mock.patch.object(guest,'bootstrap_layout'),mock.patch.object(guest,'current_path',return_value=self.old),mock.patch.object(guest,'schema_version',return_value={'schema_version':1}),mock.patch.object(guest,'backup',return_value={'path':'snapshot'}),mock.patch.object(guest,'compose') as compose,mock.patch.object(guest,'wait_ready',side_effect=[RuntimeError('not ready'),{'sampling_progressed':True}]),mock.patch.object(guest,'point') as point:
            with self.assertRaises(RuntimeError):guest.activate(self.new.name)
            self.assertEqual(compose.call_count,2)
            self.assertEqual(compose.call_args_list[1].args[0],self.old)
            self.assertIn('DASHBOARD_IMAGE=legacy',(self.root/'shared/server.env').read_text())
            self.assertEqual(point.call_args.args,('current',self.old))
            self.assertTrue(json.loads((self.root/'private/last-failed-release.json').read_text())['rollback_verified'])

    def test_success_keeps_broker_out_of_compose_recreate(self):
        with mock.patch.object(guest,'bootstrap_layout'),mock.patch.object(guest,'current_path',return_value=self.old),mock.patch.object(guest,'schema_version',return_value={'schema_version':1}),mock.patch.object(guest,'backup',return_value={'path':'snapshot'}),mock.patch.object(guest,'compose') as compose,mock.patch.object(guest,'wait_ready',return_value={'sampling_progressed':True}),mock.patch.object(guest,'point') as point,mock.patch.object(guest,'install_unit'):
            result=guest.activate(self.new.name)
            self.assertTrue(result['activated'])
            self.assertEqual(compose.call_args.args[-1],'dashboard')
            self.assertIn('--no-deps',compose.call_args.args)
            self.assertNotIn('broker',compose.call_args.args)


class TransportTests(unittest.TestCase):
    def test_service_transport_has_exact_source_and_pinned_private_identity(self):
        command=transport.service_command('id -u')
        self.assertIn('10.89.0.1',command);self.assertIn('warehouse@192.168.30.10',command)
        self.assertIn('StrictHostKeyChecking=yes',command);self.assertIn('GlobalKnownHostsFile=/dev/null',command)
        self.assertIn('maintenance_ed25519',command);self.assertIn('PasswordAuthentication=no',command)
        self.assertNotIn('192.168.200.60',command)

    def test_cloud_private_key_is_not_loaded_on_the_host(self):
        client=mock.Mock()
        with transport.ServiceRelay(client) as relay:
            with mock.patch.object(transport,'exchange',return_value=(0,'ok')) as exchange:
                self.assertEqual(relay.run('true'),(0,'ok'))
                self.assertEqual(exchange.call_args.args[0],client)
        client.close.assert_called_once()

    def test_invalid_inputs_fail_before_opening_transport(self):
        client=mock.Mock()
        with self.assertRaises(ValueError):transport.exchange(client,'true',input_data=b'a',input_file='b')
        client.get_transport.assert_not_called()

    def test_inactive_transport_is_rejected(self):
        client=mock.Mock();client.get_transport.return_value=None
        with self.assertRaises(ConnectionError):transport.exchange(client,'true')

    def fake_client(self,stdout=b'answer',stderr=b'diagnostic',code=0):
        class Channel:
            def __init__(self):self.output=stdout;self.error=stderr;self.inputs=[];self.closed=False
            def settimeout(self,value):pass
            def exec_command(self,value):self.command=value
            def sendall(self,value):self.inputs.append(value)
            def shutdown_write(self):pass
            def recv_ready(self):return bool(self.output)
            def recv(self,size):value=self.output[:size];self.output=self.output[size:];return value
            def recv_stderr_ready(self):return bool(self.error)
            def recv_stderr(self,size):value=self.error[:size];self.error=self.error[size:];return value
            def exit_status_ready(self):return not self.output and not self.error
            def recv_exit_status(self):return code
            def close(self):self.closed=True
        channel=Channel();client=mock.Mock()
        client.get_transport.return_value.open_session.return_value=channel
        return client,channel

    def test_streams_upload_and_drains_large_stderr_with_bounded_tail(self):
        client,channel=self.fake_client(stderr=b'x'*300000+b'end')
        code,tail=transport.exchange(client,'true',input_data=b'a'*200000,tail_limit=100)
        self.assertEqual(code,0);self.assertEqual(b''.join(channel.inputs),b'a'*200000)
        self.assertLessEqual(len(tail),100);self.assertTrue(tail.endswith('end'));self.assertTrue(channel.closed)

    def test_download_is_binary_exact_and_refuses_existing_destination(self):
        client,channel=self.fake_client(stdout=bytes(range(256))*1000,stderr=b'')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'backup.sqlite3'
            code,tail=transport.exchange(client,'cat',output_file=path)
            self.assertEqual(code,0);self.assertEqual(path.read_bytes(),bytes(range(256))*1000)
            with self.assertRaises(FileExistsError):transport.exchange(client,'cat',output_file=path)

    def test_transfer_failure_closes_channel(self):
        client,channel=self.fake_client()
        channel.exec_command=mock.Mock(side_effect=OSError('broken'))
        with self.assertRaises(OSError):transport.exchange(client,'true')
        self.assertTrue(channel.closed)


if __name__=='__main__':unittest.main()
