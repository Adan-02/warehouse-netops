"""No-network/offline checks for first-install disk creation guards."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
import prepare_disks as disks


class DiskPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.patch=mock.patch.object(disks,'VMS',self.root/'vms');self.patch.start();self.addCleanup(self.patch.stop)
        self.stop=mock.patch.object(disks,'ensure_stopped',side_effect=lambda role:self.root/'vms'/role/(role+'.vmx'));self.stop.start();self.addCleanup(self.stop.stop)

    def test_config_seed_disk_and_partial_outputs_are_protected(self):
        for name in ('service.vmx','seed','seed.iso','service.vmdk','service.prepare.qcow2','service.vmdk.partial','.prepare-disk.lock','.prepare-disk.work'):
            with self.subTest(name=name):
                folder=self.root/'vms/service';folder.mkdir(parents=True,exist_ok=True)
                path=folder/name;path.write_bytes(b'protected')
                with self.assertRaises(FileExistsError):disks.preflight('service')
                self.assertEqual(path.read_bytes(),b'protected');path.unlink()

    def fake_tool(self,*args):
        if args[:2]==('qemu-img','convert'):
            output=self.root/args[-1].removeprefix('/work/')
            output.write_bytes(b'sparse-image')

    def test_convert_resizes_only_a_copy_and_atomically_publishes_disk(self):
        base=self.root/'base.qcow2';base.write_bytes(b'official-source')
        with mock.patch.object(disks,'tool',side_effect=self.fake_tool) as tool:
            result=disks.prepare('service',base)
        self.assertEqual(result['capacity_gib'],16)
        self.assertEqual(base.read_bytes(),b'official-source')
        resize=tool.call_args_list[1].args
        self.assertEqual(resize,('qemu-img','resize','/work/vms/service/service.prepare.qcow2','16G'))
        self.assertEqual((self.root/'vms/service/service.vmdk').read_bytes(),b'sparse-image')
        self.assertFalse((self.root/'vms/service/.prepare-disk.lock').exists())

    def test_failure_retains_partial_files_and_does_not_publish_disk(self):
        base=self.root/'base.qcow2'
        def fail(*args):
            self.fake_tool(*args)
            if args[:2]==('qemu-img','resize'):raise RuntimeError('tool failed')
        with mock.patch.object(disks,'tool',side_effect=fail),self.assertRaises(RuntimeError):disks.prepare('iot',base)
        self.assertTrue((self.root/'vms/iot/iot.prepare.qcow2').is_file())
        self.assertFalse((self.root/'vms/iot/iot.vmdk').exists())
        self.assertFalse((self.root/'vms/iot/.prepare-disk.lock').exists())

    def test_racing_final_disk_is_never_overwritten(self):
        def tool(*args):
            self.fake_tool(*args)
            if args[:2]==('qemu-img','convert') and '.prepare-disk.work/' in args[-1]:
                (self.root/'vms/office/office.vmdk').write_bytes(b'concurrent-disk')
        with mock.patch.object(disks,'tool',side_effect=tool),self.assertRaises(FileExistsError):disks.prepare('office',self.root/'base.qcow2')
        self.assertEqual((self.root/'vms/office/office.vmdk').read_bytes(),b'concurrent-disk')

    def test_all_role_preflight_happens_before_any_preparation(self):
        folder=self.root/'vms/office';folder.mkdir(parents=True);(folder/'office.vmx').write_text('existing')
        with mock.patch.object(disks,'prepare') as prepare,mock.patch.object(disks,'verify_base') as verify,self.assertRaises(FileExistsError):disks.main([])
        prepare.assert_not_called();verify.assert_not_called()

    def test_locked_source_checksum_is_required(self):
        image=self.root/'debian-13-generic-amd64.qcow2';image.write_bytes(b'wrong')
        with mock.patch.object(disks,'IMAGES',self.root),mock.patch.object(disks,'load_lock',return_value=[{'filename':image.name,'algorithm':'sha256','checksum':'0'*64}]),self.assertRaises(RuntimeError):disks.verify_base()


if __name__=='__main__':unittest.main()
