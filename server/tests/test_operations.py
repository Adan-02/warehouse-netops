"""Behavior-level tests for durable incidents, data quality and diagnosis."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from app.config import defaults,validate
from app.main import create_app
from app.observations import ObservationReader
from app.state import SCHEMA_VERSION,TelemetryStore,iso_timestamp

BOOT_A='a'*32
BOOT_B='b'*32


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'data.sqlite3'
        self.now=1700000000.0
        self.monotonic=500.0
        self.store=self.open()

    def open(self,config=None):
        return TelemetryStore(self.path,clock=lambda:self.now,monotonic=lambda:self.monotonic,config=config)

    def advance(self,seconds):
        self.now+=seconds
        self.monotonic+=seconds

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def payload(self,seq=1,boot=BOOT_A,temperature=25,humidity=50):
        value={'device_id':'cabinet-01','seq':seq,'temperature_c':temperature}
        if boot is not None:value['boot_id']=boot
        if humidity is not None:value['humidity_pct']=humidity
        return json.dumps(value).encode()

    def count(self):
        return self.store.db.execute('SELECT COUNT(*) FROM readings').fetchone()[0]

    def test_duplicate_after_twenty_five_seconds_does_not_refresh_or_insert(self):
        self.store.ingest(self.payload())
        first=self.store.status()['last_seen_at']
        self.advance(25)
        self.assertEqual(self.store.ingest(self.payload())['reason'],'duplicate')
        self.assertEqual(self.count(),1)
        self.assertEqual(self.store.status()['last_seen_at'],first)
        self.assertEqual(self.store.status()['data_status'],'stale')
        self.assertEqual(self.store.status()['counters']['duplicates'],1)

    def test_conflicting_identity_rolls_back_and_does_not_refresh(self):
        self.store.ingest(self.payload())
        self.advance(25)
        with self.assertRaises(ValueError):self.store.ingest(self.payload(temperature=30))
        self.assertEqual(self.count(),1)
        self.assertEqual(self.store.status()['data_status'],'stale')
        self.assertEqual(self.store.status()['counters']['conflicts'],1)

    def test_new_boot_restarts_sequence_closed_session_cannot_take_over(self):
        self.store.ingest(self.payload(seq=20))
        self.advance(1)
        self.store.ingest(self.payload(seq=1,boot=BOOT_B))
        self.advance(25)
        self.assertEqual(self.store.ingest(self.payload(seq=21))['reason'],'closed_session')
        self.assertEqual(self.store.status()['boot_id'],BOOT_B)
        self.assertEqual(self.store.status()['data_status'],'stale')
        self.assertEqual(self.count(),2)

    def test_out_of_order_and_gaps_are_counted_without_guessing_loss(self):
        self.store.ingest(self.payload(seq=2))
        self.store.ingest(self.payload(seq=5))
        self.advance(25)
        self.assertEqual(self.store.ingest(self.payload(seq=4))['reason'],'out_of_order')
        status=self.store.status()
        self.assertEqual(status['counters']['gaps'],2)
        self.assertEqual(status['counters']['out_of_order'],1)
        self.assertEqual(status['data_status'],'stale')

    def test_boot_validation_and_legacy_compatibility(self):
        for boot in ('A'*32,'a'*31,'../not-a-boot','',False):
            with self.subTest(boot=boot),self.assertRaises(ValueError):self.store.ingest(self.payload(boot=boot))
        self.store.ingest(self.payload(seq=99,boot=None))
        self.store.ingest(self.payload(seq=1,boot=None))
        self.assertFalse(self.store.status()['dedup_available'])
        self.assertEqual(self.count(),2)

    def test_sessions_survive_restart_and_migration_is_additive(self):
        self.store.ingest(self.payload())
        self.store.close()
        self.store=self.open()
        self.advance(25)
        self.assertEqual(self.store.ingest(self.payload())['reason'],'duplicate')
        self.assertEqual(self.count(),1)
        self.assertEqual(self.store.db.execute("SELECT value FROM schema_meta WHERE name='schema_version'").fetchone()[0],str(SCHEMA_VERSION))
        # Old version's exact INSERT remains valid after the migration.
        self.store.db.execute('INSERT INTO readings(seq,temperature_c,humidity_pct,received_at) VALUES (2,23,45,?)',(self.now,))
        self.store.db.commit()

    def test_future_schema_is_rejected_not_downgraded(self):
        self.store.db.execute("UPDATE schema_meta SET value='999' WHERE name='schema_version'")
        self.store.db.commit()
        with self.assertRaises(ValueError):self.open()
        self.assertEqual(self.store.db.execute("SELECT value FROM schema_meta WHERE name='schema_version'").fetchone()[0],'999')

    def test_storage_failure_is_independent_and_health_does_not_read_database(self):
        self.store.set_broker_connected(True)
        self.store.ingest(self.payload())
        last=self.store.status()['last_persist_success_at']
        self.store.db.execute('PRAGMA query_only=ON')
        with self.assertRaises(sqlite3.OperationalError):self.store.ingest(self.payload(seq=2))
        client=create_app(self.store).test_client()
        self.assertEqual(client.get('/healthz').status_code,200)
        health=client.get('/api/health').json
        self.assertEqual(health['components']['storage']['state'],'unhealthy')
        self.assertEqual(health['components']['broker']['state'],'healthy')
        self.assertEqual(self.store.status()['last_persist_success_at'],last)
        self.assertEqual(self.store.status()['counters']['persist_errors'],1)
        self.store.tick()
        self.assertGreater(self.store.live_events()['events_pending_persistence'],0)
        self.store.db.execute('PRAGMA query_only=OFF')
        self.advance(1)
        self.store.ingest(self.payload(seq=2))
        self.store.tick()
        events=self.store.events()['events']
        storage=[row for row in events if row['event_type']=='storage_write_failed']
        self.assertEqual(len(storage),1)
        self.assertEqual(storage[0]['state'],'resolved')
        self.assertEqual(self.store.status()['storage_state'],'healthy')
        self.assertEqual(self.store.live_events()['events_pending_persistence'],0)

    def test_background_tick_records_stale_and_recovery_without_a_browser(self):
        self.store.set_broker_connected(True)
        self.store.ingest(self.payload())
        self.advance(21)
        self.store.tick()
        event=self.store.events()['events'][0]
        self.assertEqual(event['event_type'],'telemetry_stale')
        self.assertEqual(event['started_at'],iso_timestamp(self.now-1))
        self.store.ingest(self.payload(seq=2))
        self.store.tick()
        changes=self.store.event_changes()['changes']
        self.assertEqual([row['transition'] for row in changes],['raised','recovered'])
        self.assertEqual(changes[0]['event_id'],changes[1]['event_id'])
        self.assertGreater(changes[1]['change_id'],changes[0]['change_id'])

    def test_restart_marks_old_active_event_as_interrupted(self):
        self.store.tick()
        identity=self.store.events()['events'][0]['event_id']
        self.store.close()
        self.advance(120)
        self.store=self.open()
        event=self.store.events()['events'][0]
        self.assertEqual(event['event_id'],identity)
        self.assertEqual(event['state'],'interrupted')
        self.assertEqual(event['end_reason'],'observation_interrupted')
        self.assertEqual(self.store.event_changes()['changes'][-1]['transition'],'interrupted')

    def test_events_default_is_recent_and_explicit_cursor_is_ascending(self):
        for _ in range(4):
            self.store.set_broker_connected(False);self.store.tick()
            self.advance(1)
            self.store.set_broker_connected(True);self.store.tick()
        identities=[row['id'] for row in self.store.events(limit=20)['events']]
        self.assertEqual([row['id'] for row in self.store.events(limit=2)['events']],identities[-2:])
        self.assertEqual([row['id'] for row in self.store.events(after_id=identities[0],limit=2)['events']],identities[1:3])

    def test_threshold_sustained_recovery_hysteresis_and_missing_data(self):
        config=defaults();config['profile']='simulated_demo';config['alerts']['enabled']=True
        self.store.config=config
        self.store.set_broker_connected(True)
        for index in range(7):
            self.store.ingest(self.payload(seq=index+1,temperature=30))
            self.store.tick()
            self.advance(5)
        threshold=[row for row in self.store.events()['events'] if row['event_type']=='threshold_exceeded']
        self.assertEqual(len(threshold),1)
        self.assertEqual(threshold[0]['state'],'active')
        self.advance(25);self.store.tick()
        self.assertEqual([row for row in self.store.events()['events'] if row['event_type']=='threshold_exceeded'][0]['recovery_observation'],'unknown')
        # Inside the range but above the high recovery boundary 27 stays active.
        for index in range(8,16):
            self.store.ingest(self.payload(seq=index,temperature=27.5));self.store.tick();self.advance(5)
        self.assertTrue(any(row['event_type']=='threshold_exceeded' for row in self.store.live_events()['active_events']))
        for index in range(16,23):
            self.store.ingest(self.payload(seq=index,temperature=27));self.store.tick();self.advance(5)
        threshold=[row for row in self.store.events()['events'] if row['event_type']=='threshold_exceeded']
        self.assertEqual(threshold[0]['state'],'resolved')

    def test_threshold_short_excursion_and_wall_clock_jump_do_not_trigger(self):
        config=defaults();config['alerts']['enabled']=True;self.store.config=config
        self.store.set_broker_connected(True)
        self.store.ingest(self.payload(temperature=30));self.store.tick()
        self.now+=3600
        self.store.ingest(self.payload(seq=2,temperature=30));self.store.tick()
        self.assertFalse(any(row['event_type']=='threshold_exceeded' for row in self.store.events()['events']))
        self.advance(10)
        self.store.ingest(self.payload(seq=3,temperature=25));self.store.tick()
        self.advance(30)
        self.store.ingest(self.payload(seq=4,temperature=30));self.store.tick()
        self.assertFalse(any(row['event_type']=='threshold_exceeded' for row in self.store.events()['events']))

    def test_sample_gap_resets_continuous_alarm_even_if_evaluator_did_not_run(self):
        # A suspended VM or delayed evaluator must not count an unobserved
        # twenty-second sampling gap as continuously measured high temperature.
        config=defaults();config['profile']='simulated_demo';config['alerts']['enabled']=True
        self.store.config=config
        self.store.set_broker_connected(True)
        self.store.ingest(self.payload(temperature=30));self.store.tick()
        self.advance(40)
        self.store.ingest(self.payload(seq=2,temperature=30));self.store.tick()
        self.assertFalse(any(row['event_type']=='threshold_exceeded' for row in self.store.events()['events']))
        for seq in range(3,9):
            self.advance(5)
            self.store.ingest(self.payload(seq=seq,temperature=30));self.store.tick()
        threshold=[row for row in self.store.events()['events'] if row['event_type']=='threshold_exceeded']
        self.assertEqual(len(threshold),1)
        self.assertEqual(threshold[0]['started_at'],iso_timestamp(self.now-30))
        self.advance(5)
        self.store.ingest(self.payload(seq=9,temperature=25));self.store.tick()
        self.advance(40)
        self.store.ingest(self.payload(seq=10,temperature=25));self.store.tick()
        self.assertTrue(any(row['event_type']=='threshold_exceeded' for row in self.store.live_events()['active_events']))

    def test_range_empty_buckets_timezone_and_legacy_null(self):
        self.store.ingest(self.payload(humidity=None))
        result=self.store.history_range(iso_timestamp(self.now-60),iso_timestamp(self.now+120))
        self.assertEqual(result['resolution_seconds'],60)
        populated=[row for row in result['buckets'] if row['count']]
        self.assertEqual(len(populated),1)
        self.assertIsNone(populated[0]['humidity_pct'])
        self.assertEqual(populated[0]['temperature_c']['avg'],25)
        self.assertTrue(any(row['temperature_c'] is None for row in result['buckets']))
        result=self.store.history_range(iso_timestamp(self.now-7*86400),iso_timestamp(self.now))
        self.assertEqual(result['resolution_seconds'],300)
        self.assertLessEqual(len(result['buckets']),2048)

    def test_range_and_cursor_input_limits(self):
        client=create_app(self.store).test_client()
        for endpoint in ('/api/history/range','/api/history/export.csv'):
            for params in ({'start':'2026-01-01','end':'2026-01-02'},
                           {'start':'2026-01-01T00:00:00Z','end':'2026-02-01T00:00:00Z'},
                           {'start':'2026-01-02T00:00:00Z','end':'2026-01-01T00:00:00Z'}):
                self.assertEqual(client.get(endpoint,query_string=params).status_code,400)
        for endpoint in ('/api/events?limit=201','/api/event-changes?after_id=-1','/api/event-changes?limit=x'):
            self.assertEqual(client.get(endpoint).status_code,400)

    def test_csv_export_has_original_rows_and_empty_humidity(self):
        self.store.ingest(self.payload(humidity=None))
        client=create_app(self.store).test_client()
        response=client.get('/api/history/export.csv',query_string={'start':iso_timestamp(self.now-1),'end':iso_timestamp(self.now+1)})
        self.assertEqual(response.status_code,200)
        data=response.get_data(as_text=True)
        self.assertIn('device_id,boot_id,seq,temperature_c,humidity_pct,received_at',data)
        self.assertIn('cabinet-01,'+BOOT_A+',1,25.0,,',data)

    def test_csv_limit_is_checked_before_stream_headers(self):
        self.store.db.executemany('INSERT INTO readings(seq,temperature_c,received_at) VALUES (1,20,?)',[(self.now,)]*130001)
        self.store.db.commit()
        client=create_app(self.store).test_client()
        response=client.get('/api/history/export.csv',query_string={'start':iso_timestamp(self.now-1),'end':iso_timestamp(self.now+1)})
        self.assertEqual(response.status_code,400)

    def test_retention_preserves_active_events_and_cursor_monotonicity(self):
        self.store.ingest(self.payload())
        self.store.tick()
        self.store.set_broker_connected(True);self.store.tick()
        self.store.set_broker_connected(False);self.store.tick()
        high=self.store.event_changes()['latest_change_id']
        self.advance(181*86400)
        self.store._cleanup()
        self.assertEqual(self.count(),0)
        self.assertEqual(len(self.store.events()['events']),1)
        self.assertEqual(self.store.events()['events'][0]['state'],'active')
        self.assertEqual(self.store.event_changes()['latest_change_id'],high)

    def test_retention_uses_multiple_batches_and_never_removes_fresh_rows(self):
        old=self.now-91*86400
        self.store.db.executemany('INSERT INTO readings(seq,temperature_c,received_at) VALUES (1,20,?)',[(old,)]*17000)
        self.store.db.commit()
        self.store.ingest(self.payload())
        self.assertTrue(self.store._cleanup())
        self.assertEqual(self.count(),1)

    def test_background_thread_evaluates_without_http_requests(self):
        self.store.start_background()
        end=time.monotonic()+3
        while time.monotonic()<end:
            if self.store.events()['events']:break
            time.sleep(.05)
        self.assertTrue(self.store.events()['events'])
        self.store.close()
        self.assertFalse(self.store._thread.is_alive())

    def test_health_liveness_is_independent_of_read_queries_and_version(self):
        client=create_app(self.store).test_client()
        with mock.patch.dict(os.environ,{'APP_VERSION':'0.5.0','GIT_COMMIT':'abc','SOURCE_SHA256':'def'}):
            self.assertEqual(client.get('/api/version').json['version'],'0.5.0')
        self.store.db.close()
        self.assertEqual(client.get('/healthz').status_code,200)
        self.assertEqual(client.get('/api/status').status_code,200)
        self.assertEqual(client.get('/api/health').status_code,200)
        self.assertEqual(client.get('/api/history').status_code,503)
        self.store._closed=True


class ConfigurationTests(unittest.TestCase):
    def test_demo_thresholds_do_not_leak_into_physical_profile(self):
        template=json.loads((Path(__file__).resolve().parents[1]/'config.example.json').read_text())
        self.assertTrue(validate(template,demo_mode=True)['alerts']['enabled'])
        self.assertFalse(validate(template,demo_mode=False)['alerts']['enabled'])
        template['profile']='custom'
        self.assertTrue(validate(template,demo_mode=False)['alerts']['enabled'])

    def test_invalid_settings_rejected_at_startup(self):
        for value in ({'profile':'unknown'},{'retention':{'readings_days':False}},
                      {'alerts':{'enabled':'yes'}},{'alerts':{'trigger_seconds':0}},
                      {'alerts':{'temperature_c':{'min':30,'max':20,'hysteresis':1}}},
                      {'alerts':{'humidity_pct':{'min':30,'max':70,'hysteresis':30}}}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate(value,True)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name)
        self.now=1700000000.0
        self.reader=ObservationReader(self.directory,clock=lambda:self.now)

    def tearDown(self):self.temp.cleanup()

    def write(self,filename='local.json',observer='site',component='wan',state='healthy',received=None):
        stamp=self.now if received is None else received
        value={'schema_version':1,'observer_id':observer,'observed_at':iso_timestamp(stamp),
               'received_at':iso_timestamp(stamp),'components':{component:{'state':state,'observed_at':iso_timestamp(stamp),'evidence':{'up':True}}}}
        target=self.directory/filename
        target.write_text(json.dumps(value))
        os.utime(target,(stamp,stamp))
        return target

    def test_fresh_observation_expires_without_green_fallback(self):
        self.write()
        self.assertEqual(self.reader.read()['wan']['state'],'healthy')
        self.now+=36
        self.assertEqual(self.reader.read()['wan']['state'],'unknown')

    def test_restart_requires_new_snapshot(self):
        target=self.write()
        restarted=ObservationReader(self.directory,clock=lambda:self.now)
        self.assertEqual(restarted.read()['wan']['state'],'unknown')
        self.now+=1
        self.write()
        self.assertEqual(restarted.read()['wan']['state'],'healthy')

    def test_wrong_observer_future_time_and_large_files_are_unknown(self):
        self.write(observer='guangzhou')
        self.assertEqual(self.reader.read()['wan']['state'],'unknown')
        self.write(received=self.now+60)
        self.assertEqual(self.reader.read()['wan']['state'],'unknown')
        (self.directory/'local.json').write_text('x'*32769)
        self.assertEqual(self.reader.read()['wan']['state'],'unknown')

    def test_remote_observation_does_not_override_site_wireguard(self):
        self.write(component='wireguard',state='healthy')
        self.write('remote.json','guangzhou','wireguard','unhealthy')
        values=self.reader.read()
        self.assertEqual(values['wireguard']['state'],'healthy')
        self.assertEqual(values['remote_wireguard']['state'],'unhealthy')

    def test_symlink_is_rejected(self):
        original=self.write('real.json')
        try:(self.directory/'local.json').symlink_to(original)
        except OSError:self.skipTest('platform disallows symlink creation')
        self.assertEqual(self.reader.read()['wan']['state'],'unknown')


if __name__=='__main__':unittest.main()
