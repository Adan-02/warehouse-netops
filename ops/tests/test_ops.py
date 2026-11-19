"""Offline observer/receiver/outbox tests; no VM, router or cloud access."""
import copy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from ops.common import atomic_json, component, iso_time, private_config, validate_snapshot
from ops.receiver import receive
from ops.observer import parse_router, site_once, cloud_once, ssh_arguments
from ops.webhook import DeliveryStore, destination, notification, send

PEER='AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE='
NOW=1000


def snapshot(role='guangzhou',at=NOW):
    names=('remote_http','wireguard') if role=='guangzhou' else ('wan','wireguard')
    return {'schema_version':1,'observer_id':role,'observed_at':iso_time(at),
        'components':{name:component('healthy',{'checked':True},iso_time(at)) for name in names}}


def configuration():
    return {'enabled':True,'url':'https://hooks.example.test/warehouse', 'secret':'test-signature-secret-1234'}


class SnapshotTests(unittest.TestCase):
    def test_receiver_stamps_its_own_received_time(self):
        value=snapshot();value['received_at']=iso_time(1)
        result=validate_snapshot(value,'guangzhou',now=NOW)
        self.assertEqual(result['received_at'],iso_time(NOW))

    def test_stale_future_wrong_observer_and_unsafe_fields_rejected(self):
        variants=[snapshot(at=NOW-36),snapshot(at=NOW+6),snapshot('site')]
        value=snapshot();value['components']['remote_http']['evidence']={'password':'not-permitted'};variants.append(value)
        value=snapshot();value['components']['remote_http']['evidence']={'latency':float('nan')};variants.append(value)
        value=snapshot();value['components']['remote_http']['state']='green';variants.append(value)
        for value in variants:
            with self.subTest(value=value),self.assertRaises(ValueError):validate_snapshot(value,'guangzhou',now=NOW)

    def test_unknown_is_explicitly_supported(self):
        value=snapshot();value['components']['wireguard']['state']='unknown'
        self.assertEqual(validate_snapshot(value,'guangzhou',now=NOW)['components']['wireguard']['state'],'unknown')

    def test_receiver_fixed_protocol_and_source(self):
        env={'SSH_CONNECTION':'10.89.0.1 40000 192.168.30.10 22','SSH_ORIGINAL_COMMAND':'warehouse-report-v1'}
        with tempfile.TemporaryDirectory() as directory:
            result=receive(io.BytesIO(json.dumps(snapshot()).encode()),directory,env,NOW)
            self.assertTrue(result['accepted'])
            stored=json.loads((Path(directory)/'remote.json').read_text())
            self.assertEqual(stored['received_at'],iso_time(NOW))
            for field,value in [('SSH_ORIGINAL_COMMAND','sh'),('SSH_CONNECTION','10.77.0.1 40000 192.168.30.10 22')]:
                altered=dict(env);altered[field]=value
                with self.assertRaises(ValueError):receive(io.BytesIO(b'{}'),directory,altered,NOW)
            with self.assertRaises(ValueError):receive(io.BytesIO(b'x'*32769),directory,env,NOW)

    def test_receiver_rejects_extra_top_level_fields(self):
        value=snapshot();value['output_file']='/tmp/untrusted'
        with self.assertRaises(ValueError):validate_snapshot(value,'guangzhou',now=NOW)

    def test_atomic_output_and_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json';atomic_json(path,{'a':1},0o600)
            self.assertEqual(private_config(path),{'a':1})
            atomic_json(path,{'a':2},0o600)
            self.assertEqual(private_config(path),{'a':2})
            self.assertEqual(list(Path(directory).glob('.observation-*')),[])

    @unittest.skipIf(os.name=='nt','Windows test account may not create symlinks')
    def test_symlink_outputs_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'target';path.write_text('{}')
            link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):atomic_json(link,{'bad':True})
            with self.assertRaises(ValueError):private_config(link)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'outbox.sqlite3';self.now=NOW
        self.store=DeliveryStore(self.path,clock=lambda:self.now)
        self.config=configuration()

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def page(self,*ids,latest=None):
        rows=[{'change_id':i,'event_id':'event-one','transition':'raised' if i%2 else 'recovered',
            'occurred_at':iso_time(self.now),'event':{'event_type':'telemetry_stale','evidence':{'seconds':20}}} for i in ids]
        return {'changes':rows,'next_after_id':ids[-1] if ids else self.store.cursor,'latest_change_id':latest if latest is not None else max(ids,default=self.store.cursor)}

    def test_first_enable_skips_historical_notifications(self):
        self.assertEqual(self.store.accept_changes(self.page(1,2,latest=12),self.config),0)
        self.assertEqual(self.store.cursor,12)
        self.assertEqual(self.store.summary()['deliveries'],{})

    def test_durable_cursor_covers_both_raise_and_recovery(self):
        self.store.initialize(0,self.config)
        self.assertEqual(self.store.accept_changes(self.page(1,2),self.config),2)
        self.assertEqual(self.store.cursor,2)
        self.store.close();self.store=DeliveryStore(self.path,clock=lambda:self.now)
        self.assertEqual(self.store.cursor,2)
        payloads=[json.loads(row[0]) for row in self.store.db.execute('SELECT payload FROM deliveries ORDER BY rowid')]
        self.assertEqual([r['transition'] for r in payloads],['raised','recovered'])
        self.assertNotEqual(payloads[0]['delivery_id'],payloads[1]['delivery_id'])

    def test_invalid_second_change_rolls_back_enqueue_and_cursor(self):
        self.store.initialize(0,self.config)
        value=self.page(1,2);value['changes'][1]['change_id']=1
        with self.assertRaises(ValueError):self.store.accept_changes(value,self.config)
        self.assertEqual(self.store.cursor,0);self.assertEqual(self.store.summary()['deliveries'],{})

    def test_disabled_notifications_do_not_enqueue(self):
        self.store.accept_changes(self.page(1,2),{'enabled':False})
        self.assertEqual(self.store.cursor,2)
        self.assertFalse(self.store.dispatch({'enabled':False})['enabled'])

    def test_destination_change_cannot_send_old_queue_to_new_destination(self):
        self.store.initialize(0,self.config);self.store.accept_changes(self.page(1),self.config)
        altered=dict(self.config,url='https://different.example.test/hooks')
        result=self.store.dispatch(altered,sender=lambda *args: self.fail('Old queue leaked to different destination'))
        self.assertEqual(result['processed'],0)

    def test_retry_keeps_same_id_across_restart_and_is_bounded(self):
        self.store.initialize(0,self.config);self.store.accept_changes(self.page(1),self.config)
        identities=[]
        def failed(payload,identity,config):
            identities.append(identity);return {'state':'retry','http_status':503}
        for index in range(8):
            self.store.dispatch(self.config,sender=failed)
            self.now+=1000
        self.assertEqual(len(set(identities)),1)
        self.assertEqual(self.store.summary()['deliveries'],{'failed':1})
        self.store.dispatch(self.config,sender=failed)
        self.assertEqual(len(identities),8)

    def test_2xx_confirmation_removes_message_from_due_queue(self):
        self.store.initialize(0,self.config);self.store.accept_changes(self.page(1),self.config)
        self.store.dispatch(self.config,sender=lambda *args:{'state':'sent','http_status':204})
        self.assertEqual(self.store.summary()['deliveries'],{'sent':1})
        result=self.store.dispatch(self.config,sender=lambda *args:self.fail('Sent message was redelivered'))
        self.assertEqual(result['processed'],0)

    def test_remote_fault_survives_site_unavailability_and_only_one_active_event(self):
        self.store.observe_remote(False,self.config);self.store.observe_remote(False,self.config)
        self.assertTrue(self.store.summary()['remote_event_active'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM remote_events').fetchone()[0],1)
        self.now+=30;self.store.observe_remote(True,self.config)
        self.assertFalse(self.store.summary()['remote_event_active'])
        self.assertEqual(self.store.summary()['deliveries'],{'pending':2})

    def test_daily_backup_is_consistent_and_keeps_seven(self):
        for day in range(10):
            self.now=NOW+86400*day
            target=self.store.daily_backup(Path(self.temp.name)/'backups')
            copy=sqlite3.connect(target)
            try:self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:copy.close()
        self.assertEqual(len(list((Path(self.temp.name)/'backups').glob('*.sqlite3'))),7)


class WebhookTests(unittest.TestCase):
    def test_destination_validation(self):
        self.assertIsNone(destination({'enabled':False}))
        for url in ('http://public.example.test/hook','https://name:secret@example.test/hook','https://example.test/hook#frag'):
            with self.subTest(url=url),self.assertRaises(ValueError):destination(dict(configuration(),url=url))
        with self.assertRaises(ValueError):destination(dict(configuration(),secret='short'))
        with self.assertRaises(ValueError):destination(dict(configuration(),url='http://127.0.0.1/test'))
        self.assertIsNotNone(destination(dict(configuration(),url='http://127.0.0.1/test',allow_loopback_http_for_tests=True)))

    def server(self,status=204,retry_after=None):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append({'headers':dict(self.headers),'body':self.rfile.read(int(self.headers['Content-Length']))})
                self.send_response(status)
                if retry_after:self.send_header('Retry-After',str(retry_after))
                if status==302:self.send_header('Location','http://127.0.0.1:9/must-not-follow')
                self.end_headers()
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return server,received

    def test_loopback_signature_and_idempotency_key(self):
        server,received=self.server()
        config=dict(configuration(),url='http://127.0.0.1:'+str(server.server_port)+'/hook',allow_loopback_http_for_tests=True)
        payload=b'{"event":"sample"}'
        self.assertEqual(send(payload,'delivery-one',config)['state'],'sent')
        self.assertEqual(received[0]['headers']['Idempotency-Key'],'delivery-one')
        self.assertEqual(received[0]['headers']['X-Warehouse-Signature'],'sha256='+hmac.new(config['secret'].encode(),payload,hashlib.sha256).hexdigest())
        self.assertEqual(received[0]['body'],payload)

    def test_redirect_is_not_followed(self):
        server,received=self.server(302)
        config=dict(configuration(),url='http://127.0.0.1:'+str(server.server_port),allow_loopback_http_for_tests=True)
        result=send(b'{}','id',config)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['http_status'],302)
        self.assertEqual(len(received),1)

    def test_429_and_5xx_retry(self):
        for code in (429,503):
            server,received=self.server(code,7)
            config=dict(configuration(),url='http://127.0.0.1:'+str(server.server_port),allow_loopback_http_for_tests=True)
            result=send(b'{}','id',config)
            self.assertEqual(result['state'],'retry');self.assertEqual(result['retry_after'],7)

    def test_timeout_is_retry_without_credential_or_url_echo(self):
        with mock.patch('ops.webhook.http.client.HTTPSConnection') as cls:
            cls.return_value.request.side_effect=TimeoutError('https://sensitive-url/example')
            result=send(b'{}','id',configuration())
        self.assertEqual(result,{'state':'retry','reason':'TimeoutError'})
        self.assertNotIn('sensitive-url',json.dumps(result))


def router_transcript():
    parts=['WH_OBSERVER_1','WAN',json.dumps({'up':True}),'ROUTES','default via 192.168.200.1 dev eth0','10.89.0.1 dev wg_warehouse proto static scope link','HANDSHAKES',PEER+'\t990']
    for role in ('OFFICE','IOT','WAN'):
        parts += ['COUNTERS_'+role,json.dumps({'nftables':[{'rule':{'chain':'forward_'+role.lower(),'comment':'Warehouse-forward-deny','expr':[{'counter':{'packets':4}}]}}]})]
    return '\n'.join(parts+['END'])+'\n'


class ObserverTests(unittest.TestCase):
    def test_site_parses_real_sections_without_private_material(self):
        result=parse_router(router_transcript(),PEER,NOW)
        self.assertEqual(result['components']['wan']['state'],'healthy')
        self.assertEqual(result['components']['wireguard']['state'],'healthy')
        self.assertEqual(result['components']['wan']['evidence']['counters']['iot_denied_packets'],4)

    def test_site_error_replaces_green_with_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            value=site_once({'expected_peer':PEER,'observations_dir':directory},runner=lambda *args:(_ for _ in ()).throw(TimeoutError()),clock=lambda:NOW)
            self.assertEqual({v['state'] for v in value['components'].values()},{'unknown'})
            self.assertTrue((Path(directory)/'local.json').is_file())

    def test_wrong_peer_and_old_handshake_cannot_pass(self):
        value=parse_router(router_transcript(),PEER,NOW+200)
        self.assertEqual(value['components']['wireguard']['state'],'unhealthy')
        value=parse_router(router_transcript(),'B'*43+'=',NOW)
        self.assertEqual(value['components']['wireguard']['state'],'unhealthy')

    def test_ssh_fixed_protocols_and_sources(self):
        config={'state_dir':'/var/lib/warehouse-netops-observer/private'}
        site=ssh_arguments(config);remote=ssh_arguments(config,report=True)
        self.assertEqual(site[-1],'warehouse-observe-v1');self.assertIn('192.168.30.10',site)
        self.assertEqual(remote[-1],'warehouse-report-v1');self.assertIn('10.89.0.1',remote)
        self.assertIn('StrictHostKeyChecking=yes',remote)

    def test_cloud_collects_and_queues_without_opening_network_in_test(self):
        with tempfile.TemporaryDirectory() as directory:
            config={'expected_peer':PEER,'state_dir':directory,'webhook':{'enabled':False}}
            calls=[]
            def runner(args,**kwargs):
                calls.append((args,kwargs))
                if args[-1]=='warehouse-report-v1':return '{"accepted":true}'
                return json.dumps({'peers':[PEER],'handshakes':{PEER:990},'service_route_device':'wg-warehouse'})
            def getter(path):
                if path=='/api/health':return {'status':200,'connected':True,'data':{'state':'healthy'}}
                return {'status':200,'data':{'changes':[],'next_after_id':0,'latest_change_id':0}}
            result=cloud_once(config,runner=runner,getter=getter,clock=lambda:NOW)
            self.assertTrue(result['report_delivered'])
            self.assertEqual(result['snapshot']['components']['remote_http']['state'],'healthy')
            self.assertFalse(result['notifications']['enabled'])
            self.assertTrue((Path(directory)/'outbox.sqlite3').is_file())

    def test_cloud_remote_fault_is_persisted_and_snapshot_delivery_failure_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            config={'expected_peer':PEER,'state_dir':directory,'webhook':{'enabled':False}}
            def fails(*args,**kwargs):raise TimeoutError()
            result=cloud_once(config,runner=fails,getter=fails,clock=lambda:NOW)
            self.assertFalse(result['report_delivered'])
            self.assertEqual(result['snapshot']['components']['remote_http']['state'],'unhealthy')
            self.assertTrue(result['notifications']['remote_event_active'])


class InstallerTests(unittest.TestCase):
    def test_root_unit_permissions_and_no_administrative_observer_group(self):
        from ops.install_remote import unit
        site=unit('site');cloud=unit('cloud')
        self.assertIn('User=warehouse-observer',site)
        self.assertIn('NoNewPrivileges=yes',site)
        self.assertIn('ProtectSystem=strict',cloud)
        self.assertIn('ReadWritePaths=/var/lib/warehouse-netops-observer/private',cloud)
        self.assertNotIn('docker',site+cloud)
        self.assertNotIn('User=root',site+cloud)

    def test_public_key_command_injection_is_rejected(self):
        from virtualization.install_observers import public_key
        for value in ('ssh-ed25519 AAA;echo bad','ssh-rsa AAA','ssh-ed25519 AAA\ncommand=anything'):
            with self.assertRaises(ValueError):public_key(value)

    def test_failed_remote_operation_does_not_echo_output(self):
        from virtualization.install_observers import checked
        relay=mock.Mock();relay.run.return_value=(1,'sensitive diagnostic text')
        with self.assertRaises(RuntimeError) as failure:checked(relay,'fixed-project-command')
        self.assertNotIn('sensitive diagnostic',str(failure.exception))

    def test_full_install_is_gated_by_protected_baseline(self):
        from virtualization import install_observers as installer
        relay=mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            baseline=Path(directory)/'guangzhou-before.json';baseline.write_text('{"legacy":true}')
            fake_cloud=types.ModuleType('virtualization.cloud_connection');fake_cloud.audit=lambda client:{'legacy':False}
            with mock.patch.object(installer,'PRIVATE',Path(directory)),mock.patch.dict(sys.modules,{'virtualization.cloud_connection':fake_cloud}):
                with self.assertRaises(RuntimeError):installer.install(relay,console_factory=lambda role:self.fail('No console before baseline guard'))
        relay.run.assert_not_called();relay.cloud_run.assert_not_called()

    def test_generated_router_authorization_is_forced_and_does_not_change_network(self):
        from virtualization.install_observers import router_script
        text=router_script('ssh-ed25519 '+'A'*43+'= observer').decode()
        self.assertIn('command="/usr/libexec/warehouse-observer-read"',text)
        self.assertIn('no-port-forwarding',text);self.assertIn('no-pty',text)
        for dangerous in ('uci set','fw4 reload','iptables','systemctl restart','from='):
            self.assertNotIn(dangerous,text)

    def test_bundle_contains_only_owned_plain_source_files(self):
        import tarfile
        from virtualization.install_observers import bundle_bytes
        with tarfile.open(fileobj=io.BytesIO(bundle_bytes()),mode='r:gz') as archive:
            names=archive.getnames()
            self.assertIn('ops/observer.py',names)
            self.assertTrue(all(name.startswith('ops/') and not member.issym() for name,member in zip(names,archive.getmembers())))
            self.assertFalse(any('.key' in name or '.env' in name or 'config.json' in name for name in names))

    def test_router_wrapper_checks_source_and_never_exports_secrets(self):
        text=(Path(__file__).resolve().parents[1]/'router-read.sh').read_text()
        self.assertIn('SSH_CONNECTION',text);self.assertIn('192.168.30.10',text)
        self.assertIn('SSH_ORIGINAL_COMMAND',text)
        self.assertNotIn('showconf',text);self.assertNotIn(' dump',text)
        self.assertNotIn('uci ',text);self.assertNotIn('eval ',text)


if __name__=='__main__':
    unittest.main()
