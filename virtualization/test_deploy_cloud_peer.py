"""Pure local guard/failure tests; no SSH connection or VM access."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deploy_cloud_peer as deploy


class DeployGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.private = self.project/'.secrets/virtual-lab'
        self.private.mkdir(parents=True)
        (self.project/'evidence').mkdir()
        self.patch_private = mock.patch.object(deploy,'PRIVATE',self.private)
        self.patch_project = mock.patch.object(deploy,'PROJECT',self.project)
        self.patch_private.start()
        self.patch_project.start()
        self.addCleanup(self.patch_private.stop)
        self.addCleanup(self.patch_project.stop)
        self.before = {'units': {'wg0':'active'}, 'hashes':'same'}
        (self.private/'guangzhou-before.json').write_text(json.dumps(self.before),encoding='utf-8')
        self.state = {'bundle_id':'0'*32,'remote_bundle':'/root/warehouse-netops-deploy/'+'0'*32}

    def test_pair_is_reused_and_mismatch_refused(self):
        deploy.keypair('router-wg')
        secret = (self.private/'router-wg.key').read_bytes()
        deploy.keypair('router-wg')
        self.assertEqual(secret,(self.private/'router-wg.key').read_bytes())
        (self.private/'router-wg.pub').write_bytes(base64.b64encode(bytes(range(32)))+b'\n')
        with self.assertRaisesRegex(RuntimeError,'does not match'):
            deploy.keypair('router-wg')
        self.assertEqual(secret,(self.private/'router-wg.key').read_bytes())

    def test_orphan_public_refuses_new_private(self):
        (self.private/'router-wg.pub').write_text('existing')
        with self.assertRaisesRegex(RuntimeError,'refusing replacement'):
            deploy.keypair('router-wg')
        self.assertFalse((self.private/'router-wg.key').exists())

    def test_matrix_gate_rejects_inconclusive_or_wrong_phase(self):
        path = self.project/'evidence/virtual-matrix.json'
        for value in ({'phase':'baseline','passed':False},{'phase':'restore','passed':True}):
            path.write_text(json.dumps(value),encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError,'passing real local'):
                deploy.require_matrix()
        path.write_text(json.dumps({'phase':'baseline','passed':True}),encoding='utf-8')
        deploy.require_matrix()

    def test_changed_legacy_baseline_prevents_upload(self):
        with mock.patch.object(deploy,'audit',return_value={'changed':True}), mock.patch.object(deploy,'upload_bundle') as upload:
            with self.assertRaisesRegex(RuntimeError,'baseline changed'):
                deploy.install(object(),self.private,self.state)
            upload.assert_not_called()

    def test_preflight_failure_does_not_invoke_install_or_rollback(self):
        with mock.patch.object(deploy,'audit',return_value=self.before), mock.patch.object(deploy,'upload_bundle'), mock.patch.object(deploy,'remote_command',side_effect=RuntimeError('no preflight')), mock.patch.object(deploy,'exec_collect') as collect:
            with self.assertRaises(RuntimeError):
                deploy.install(object(),self.private,self.state)
            collect.assert_not_called()
        evidence=json.loads((self.project/'evidence/guangzhou-deployment.json').read_text())
        self.assertFalse(evidence['preflight_passed'])
        self.assertFalse(evidence['rollback_attempted'])
        self.assertTrue(evidence['legacy_baseline_unchanged_after_failure'])

    def test_install_failure_rolls_back_only_own_bundle(self):
        with mock.patch.object(deploy,'audit',return_value=self.before), mock.patch.object(deploy,'upload_bundle'), mock.patch.object(deploy,'remote_command',side_effect=['check passed',RuntimeError('failed')]), mock.patch.object(deploy,'exec_collect',return_value=(0,'removed own resources')) as collect:
            with self.assertRaises(RuntimeError):
                deploy.install(object(),self.private,self.state)
            self.assertIn('install.sh rollback --project-dir /root/warehouse-netops-deploy/',collect.call_args.args[1])
        evidence=json.loads((self.project/'evidence/guangzhou-deployment.json').read_text())
        self.assertTrue(evidence['preflight_passed'])
        self.assertTrue(evidence['rollback_attempted'])
        self.assertTrue(evidence['rollback_succeeded'])
        self.assertNotIn('failed',(self.project/'evidence/guangzhou-deployment.json').read_text())

    def test_verified_success_preserves_baseline(self):
        observed={'units':{'newwg':{'exit_code':0}},'listen_port':{'exit_code':0,'output':'51830'},'firewall_check':{'exit_code':0},'latest_handshake_epoch':0}
        with mock.patch.object(deploy,'audit',return_value=self.before), mock.patch.object(deploy,'upload_bundle'), mock.patch.object(deploy,'remote_command',return_value='ok'), mock.patch.object(deploy,'status',return_value=observed), mock.patch.object(deploy,'exec_collect') as collect:
            report=deploy.install(object(),self.private,self.state)
            collect.assert_not_called()
        self.assertTrue(report['installed'])
        self.assertTrue(report['legacy_baseline_unchanged'])
        self.assertEqual(report['reachability'],'not_yet_verified_router_or_cloud_security_group')

    def test_remote_manifest_mismatch_refused(self):
        (self.private/'checksums.txt').write_text('expected')
        with mock.patch.object(deploy,'remote_command',return_value='wrong  checksums.txt'):
            with self.assertRaisesRegex(RuntimeError,'differs'):
                deploy.verify_uploaded_manifest(object(),self.private,'/root/example')


if __name__ == '__main__':
    unittest.main()
