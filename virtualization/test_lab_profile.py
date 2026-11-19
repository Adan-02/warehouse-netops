"""Offline profile, locked-download and non-mutating doctor tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
import lab_profile
import doctor
import download_images as downloads


class ProfileTests(unittest.TestCase):
    def test_defaults_keep_existing_lab_compatible(self):
        with mock.patch.dict(os.environ,{},clear=True),mock.patch.object(Path,'home',return_value=Path('C:/Users/tester')),mock.patch.object(Path,'exists',return_value=False),mock.patch.object(Path,'is_symlink',return_value=False):
            result=lab_profile.load_profile()
        self.assertTrue(result['runtime_dir'].endswith('Documents'+os.sep+'Codex'+os.sep+'warehouse-netops-runtime'))
        self.assertEqual(result['cloud_ssh_alias'],'tencent-qq-ops')

    def test_explicit_machine_profile_overrides_paths_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'lab.json'
            path.write_text(json.dumps({'schema_version':1,'runtime_dir':temp,'vmrun':str(Path(temp)/'vmrun.exe'),'cloud_ssh_alias':'another-owned-alias'}))
            result=lab_profile.load_profile(path)
            self.assertEqual(result['runtime_dir'],temp);self.assertEqual(result['cloud_ssh_alias'],'another-owned-alias')

    def test_environment_selects_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'lab.json';path.write_text(json.dumps({'runtime_dir':temp}))
            with mock.patch.dict(os.environ,{'WAREHOUSE_LAB_PROFILE':str(path)}):self.assertEqual(lab_profile.load_profile()['runtime_dir'],temp)

    def test_explicit_missing_profile_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temp,self.assertRaises(FileNotFoundError):lab_profile.load_profile(Path(temp)/'missing.json')

    def test_rejects_unknown_schema_keys_relative_paths_and_alias_commands(self):
        for value in ({'schema_version':True},{'schema_version':2},{'runtime_dir':'../runtime'},{'password':'secret'},{'cloud_ssh_alias':'alias; reboot'}):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/'lab.json';path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):lab_profile.load_profile(path)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.paths=mock.patch.object(downloads,'load_paths',return_value=mock.Mock(images=Path('unused-images')))
        self.paths.start();self.addCleanup(self.paths.stop)

    def fixture(self):
        return {'schema_version':1,'images':[{'url':'https://cloud.debian.org/images/test.qcow2','filename':'test.qcow2','algorithm':'sha256','checksum':'a'*64}]}

    def test_checked_in_lock_has_two_frozen_official_images(self):
        rows=downloads.load_lock()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['checksum'],'e53a93ef33f828ceed0e7278c2cc2753bdc7d5098fbfae15bd64b6d878db350ea3744247763a1c8ecb6b4019a0f623c38caf6038ad6f9c357fb4e01493ddf1a3')

    def test_bad_names_mirrors_and_checksum_shapes_are_rejected(self):
        for key,value in (('filename','../test.qcow2'),('url','http://cloud.debian.org/images/test.qcow2'),('url','https://mirror.example/images/test.qcow2'),('checksum','latest'),('algorithm','md5')):
            with self.subTest(key=key,value=value),tempfile.TemporaryDirectory() as temp:
                data=self.fixture();data['images'][0][key]=value
                path=Path(temp)/'lock.json';path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):downloads.load_lock(path)

    def test_download_uses_reviewed_lock_and_never_rewrites_old_metadata(self):
        data=self.fixture()['images']
        with mock.patch.object(downloads,'load_lock',return_value=data),mock.patch.object(downloads,'prepare_dirs'),mock.patch.object(downloads,'download') as download,mock.patch.object(Path,'write_text') as write:
            self.assertEqual(downloads.main([]),0)
            download.assert_called_once_with(data[0]['url'],Path('unused-images')/'test.qcow2','a'*64,'sha256')
            write.assert_not_called()

    def test_verify_only_cannot_download_or_create_directories(self):
        with mock.patch.object(downloads,'load_lock',return_value=self.fixture()['images']),mock.patch.object(Path,'is_file',return_value=True),mock.patch.object(downloads,'digest',return_value='a'*64),mock.patch.object(downloads,'download') as download,mock.patch.object(downloads,'prepare_dirs') as prepare,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(downloads.main(['--verify-only']),0)
            download.assert_not_called();prepare.assert_not_called()


class DoctorTests(unittest.TestCase):
    def test_active_snapshot_disk_is_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);disk=root/'service-000001.vmdk';disk.write_bytes(b'fake')
            vmx=root/'service.vmx';vmx.write_text('ethernet0.present = "TRUE"\nide0:0.fileName = "service-000001.vmdk"\nserial0.present = "TRUE"\nserial0.fileType = "pipe"\n')
            (root/'service.vmx.lck').mkdir()
            original=vmx.read_bytes()
            value=doctor.vm_config(vmx,'service',True)
            self.assertEqual(value['status'],'pass');self.assertTrue(value['active_disk_present'])
            self.assertEqual(vmx.read_bytes(),original);self.assertTrue((root/'service.vmx.lck').exists())
            self.assertEqual(doctor.vm_config(vmx,'service',False)['status'],'warning')

    def test_extra_management_nic_fails_config_readiness(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'disk.vmdk').write_bytes(b'fake')
            vmx=root/'iot.vmx';vmx.write_text('ethernet0.present = "TRUE"\nethernet1.present = "TRUE"\nide0:0.fileName = "disk.vmdk"\nserial0.present = "TRUE"\nserial0.fileType = "pipe"\n')
            self.assertEqual(doctor.vm_config(vmx,'iot',False)['status'],'failed')

    def test_command_inventory_is_read_only_and_contains_no_credentials_or_network_mutations(self):
        with tempfile.TemporaryDirectory() as temp:
            profile={'runtime_dir':temp,'vmrun':str(Path(temp)/'vmrun.exe'),'vmware_preferences':str(Path(temp)/'prefs')}
            def execute(args,**kwargs):
                if args[:2]==['git','rev-parse']:return {'exit_code':0,'output':'true'}
                return {'exit_code':0,'output':'test'}
            with mock.patch.object(doctor,'run',side_effect=execute) as runner,mock.patch.object(doctor,'load_lock',return_value=[]),mock.patch.object(Path,'mkdir') as mkdir,mock.patch.object(Path,'write_text') as write:
                report=doctor.inspect(profile)
            self.assertFalse(report['cloud_accessed']);self.assertFalse(report['credentials_read'])
            self.assertFalse(report['business_health_tested']);mkdir.assert_not_called();write.assert_not_called()
            for call in runner.call_args_list:
                self.assertNotIn('start',call.args[0]);self.assertNotIn('stop',call.args[0]);self.assertNotIn('sudo',call.args[0]);self.assertNotIn('ssh',call.args[0])


if __name__=='__main__':unittest.main()
