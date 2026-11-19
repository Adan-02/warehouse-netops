import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from install_observers import router_host_key
from install_observers import observer_status, summarize_cycle
from install_observers import upload_code, INSTALL
from unittest import mock
import json
import hashlib


class ObserverUploadTests(unittest.TestCase):
    def test_upload_keeps_location_separate_from_install_path(self):
        data=b'observer code'
        expected=INSTALL+'/'+hashlib.sha256(data).hexdigest()[:20]
        for target,method in (('site','run'),('guangzhou','cloud_run')):
            with self.subTest(target=target):
                relay=mock.Mock()
                getattr(relay,method).return_value=(0,'')
                result=upload_code(relay,data,target=target)
                self.assertEqual(result,expected)
                call=getattr(relay,method).call_args
                self.assertIn(expected+'/code.tar.gz',call.args[0])
                self.assertEqual(call.kwargs['input_data'],data)
                getattr(relay,'run' if method=='cloud_run' else 'cloud_run').assert_not_called()

    def test_default_upload_uses_site_connection(self):
        relay=mock.Mock()
        relay.run.return_value=(0,'')
        upload_code(relay,b'code')
        relay.run.assert_called_once()
        relay.cloud_run.assert_not_called()


class HostPinTests(unittest.TestCase):
    def test_echoed_sed_pattern_is_not_a_second_host_key(self):
        key='ssh-ed25519 '+'A'*68
        output="dropbearkey | sed -n '/^ssh-ed25519 /p'\r\n"+key+' root@OpenWrt\r\n'
        self.assertEqual(router_host_key(output),key)

    def test_echo_only_or_ambiguous_key_is_rejected(self):
        key='ssh-ed25519 '+'A'*68+'\n'
        for value in ("sed -n '/^ssh-ed25519 /p'\r\n",key+key):
            with self.assertRaises(RuntimeError):router_host_key(value)


class ObserverStatusTests(unittest.TestCase):
    def test_active_timer_does_not_override_stale_cycle(self):
        value={'observer_id':'site','observed_at':'1970-01-01T00:16:40+00:00',
            'stages':{'probe':{'state':'healthy','reason_code':'ok','last_success_at':'1970-01-01T00:16:40+00:00'}}}
        relay=mock.Mock()
        relay.run.side_effect=[(0,'active\n'),(0,json.dumps(value))]
        result=observer_status(relay,'site',clock=lambda:1036)
        self.assertEqual(result['timer']['state'],'active')
        self.assertEqual(result['observation']['state'],'unknown')
        self.assertEqual(result['observation']['reason_code'],'cycle_stale')
        relay.cloud_run.assert_not_called()
        commands=[call.args[0] for call in relay.run.call_args_list]
        self.assertTrue(commands[0].startswith('systemctl is-active '))
        self.assertTrue(commands[1].startswith('sudo -n /usr/bin/python3 -c '))
        for command in commands:
            self.assertNotIn('systemctl start',command)
            self.assertNotIn('install_remote',command)

    def test_unknown_or_sensitive_diagnostics_are_not_printed(self):
        value={'observer_id':'site','observed_at':'1970-01-01T00:16:40+00:00',
            'extra':'PRIVATE KEY / credential',
            'stages':{'probe':{'state':'unhealthy','reason_code':'https://private/token',
                'last_success_at':'private-token'},'unexpected':{'state':'healthy'}}}
        result=summarize_cycle(value,'site',1000)
        self.assertEqual(result['state'],'unhealthy')
        self.assertEqual(result['stages']['probe']['reason_code'],'invalid_diagnostic')
        self.assertNotIn('private',json.dumps(result))
        self.assertNotIn('unexpected',result['stages'])

    def test_missing_legacy_stages_never_claim_current_health(self):
        value={'observed_at':'1970-01-01T00:16:40+00:00','remote_http_state':'healthy','report_delivered':True}
        self.assertEqual(summarize_cycle(value,'guangzhou',1000)['reason_code'],'legacy_diagnostics_missing')


if __name__=='__main__':unittest.main()
