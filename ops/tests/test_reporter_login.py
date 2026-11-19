import importlib.util
from pathlib import Path
import unittest

from ops.install_remote import reporter_authorization
from ops.observer import ssh_arguments

spec=importlib.util.spec_from_file_location('reporter_login',Path(__file__).resolve().parents[1]/'receive-login.py')
login=importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


class ReporterLoginTests(unittest.TestCase):
    def environment(self):
        return {'SSH_CONNECTION':'10.89.0.1 45000 192.168.30.10 22',
                'SSH_ORIGINAL_COMMAND':'warehouse-report-v1'}

    def test_protocol_drops_to_low_privilege_without_shell(self):
        args=login.receiver_arguments(self.environment())
        self.assertEqual(args[:6],['/usr/bin/sudo','-n','-u','warehouse-observer','--','/usr/bin/env'])
        self.assertIn('ops.receiver',args)
        self.assertNotIn('/bin/sh',args)
        self.assertIn('warehouse@192.168.30.10',ssh_arguments({},report=True))

    def test_arbitrary_command_or_bad_connection_rejected(self):
        for field,value in [('SSH_ORIGINAL_COMMAND','id'),('SSH_ORIGINAL_COMMAND','warehouse-report-v1; id'),
            ('SSH_CONNECTION','10.77.0.1 45000 192.168.30.10 22'),
            ('SSH_CONNECTION','10.89.0.1 45000 127.0.0.1 22'),
            ('SSH_CONNECTION','10.89.0.1 $(id) 192.168.30.10 22'),
            ('SSH_CONNECTION','10.89.0.1 70000 192.168.30.10 22')]:
            environment=self.environment();environment[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):login.receiver_arguments(environment)

    def test_preserves_existing_keys_and_reinstallation_is_idempotent(self):
        existing='ssh-ed25519 '+'B'*68+' old-administrator\n'
        reporter='ssh-ed25519 '+'A'*68
        result=reporter_authorization(existing,reporter)
        self.assertIn(existing.rstrip(),result)
        self.assertEqual(reporter_authorization(result,reporter),result)
        self.assertIn('from="10.89.0.1",restrict,command=',result)
        with self.assertRaises(RuntimeError):reporter_authorization(result,'ssh-ed25519 '+'C'*68)


if __name__=='__main__':unittest.main()
