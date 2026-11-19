"""Behavioral guardrails for the smaller telemetry coordinator and time windows."""
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from app.main import create_app, make_client
from app.state import TelemetryService, TelemetryStore, iso_timestamp
from app.thresholds import PendingThreshold, evaluate_threshold, sampling_interrupted


class HistoryWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1700000000.0
        self.service = TelemetryService(Path(self.temp.name) / 'history.sqlite3', clock=lambda: self.now)
        self.client = create_app(self.service).test_client()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def ingest(self, seq):
        self.service.ingest(json.dumps({'device_id':'cabinet-01','seq':seq,'temperature_c':24,'humidity_pct':48}))

    def test_true_window_after_long_outage_and_csv_share_boundaries(self):
        self.ingest(1)
        self.now += 3600
        self.ingest(2)
        selection = {'start':iso_timestamp(self.now-600),'end':iso_timestamp(self.now+1)}
        raw = self.client.get('/api/history', query_string={**selection,'limit':240})
        export = self.client.get('/api/history/export.csv', query_string=selection)
        self.assertEqual(raw.status_code,200)
        readings = raw.json['readings']
        csv_rows = list(csv.DictReader(io.StringIO(export.get_data(as_text=True))))
        self.assertEqual([row['seq'] for row in readings],[2])
        self.assertEqual([int(row['seq']) for row in csv_rows],[2])
        # Older clients still mean "last N readings", with no implicit window.
        self.assertEqual([row['seq'] for row in self.client.get('/api/history?limit=2').json['readings']],[1,2])

    def test_half_open_bounds_limit_and_empty_window(self):
        first = self.now
        for seq in range(1,4):
            self.ingest(seq)
            self.now += 5
        query = {'start':iso_timestamp(first),'end':iso_timestamp(first+10),'limit':1}
        self.assertEqual([row['seq'] for row in self.client.get('/api/history',query_string=query).json['readings']],[2])
        empty = {'start':iso_timestamp(first+100),'end':iso_timestamp(first+200)}
        self.assertEqual(self.client.get('/api/history',query_string=empty).json['readings'],[])

    def test_paired_timezone_aware_bounded_window_is_required(self):
        start = iso_timestamp(self.now)
        for query in ({'start':start},{'end':start}, {'start':'2026-01-01','end':'2026-01-02'},
                      {'start':start,'end':iso_timestamp(self.now+8*86400)},
                      {'start':start,'end':start},{'start':start,'end':iso_timestamp(self.now+10),'limit':241}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/history',query_string=query).status_code,400)


class NamingCompatibilityTests(unittest.TestCase):
    def test_legacy_store_name_and_subscription_accessors_still_work(self):
        self.assertIs(TelemetryStore,TelemetryService)
        with tempfile.TemporaryDirectory() as temp:
            service = TelemetryService(Path(temp) / 'compat.sqlite3')
            try:
                service.set_mqtt_subscription_active(True)
                self.assertTrue(service.broker_connected)
                self.assertTrue(service.status()['broker_connected'])
                service.broker_connected = False
                self.assertFalse(service.mqtt_subscription_active)
                service.set_broker_connected(True)
                self.assertTrue(service.mqtt_subscription_active)
            finally:
                service.close()

    def test_connack_does_not_imply_suback_and_rejected_suback_stays_false(self):
        with tempfile.TemporaryDirectory() as temp:
            service = TelemetryService(Path(temp) / 'mqtt.sqlite3')
            try:
                with mock.patch.dict('os.environ',{'MQTT_PASSWORD':'test-only'}),mock.patch('app.main.mqtt.Client') as factory:
                    connection = mock.Mock()
                    factory.return_value = connection
                    connection.subscribe.return_value = (0,1)
                    make_client(service)
                    accepted = mock.Mock(is_failure=False)
                    rejected = mock.Mock(is_failure=True)
                    connection.on_connect(connection,None,None,accepted,None)
                    self.assertFalse(service.mqtt_subscription_active)
                    connection.on_subscribe(connection,None,1,[rejected],None)
                    self.assertFalse(service.mqtt_subscription_active)
                    connection.on_subscribe(connection,None,1,[accepted],None)
                    self.assertTrue(service.mqtt_subscription_active)
            finally:
                service.close()


class PureThresholdTests(unittest.TestCase):
    def decide(self,**changes):
        values = dict(value=30,direction='high',limits={'min':18,'max':28,'hysteresis':1},
                      active=False,pending=None,sample_fresh=True,now_monotonic=100,now_utc=1700000000,
                      trigger_seconds=30,recovery_seconds=30,sample_gap=False)
        values.update(changes)
        return evaluate_threshold(**values)

    def test_named_trigger_and_recovery_phases_keep_hysteresis_and_monotonic_timing(self):
        start = self.decide()
        self.assertEqual(start.action,'hold')
        self.assertEqual(start.pending,PendingThreshold(100,1700000000,'trigger'))
        raised = self.decide(pending=start.pending,now_monotonic=130,now_utc=1)
        self.assertEqual(raised.action,'raise')
        self.assertEqual(raised.started_at,1700000000)
        self.assertEqual(self.decide(active=True,value=27.5).action,'reset')
        recovery = self.decide(active=True,value=27)
        self.assertEqual(recovery.pending.phase,'recovery')
        self.assertEqual(self.decide(active=True,value=27,pending=recovery.pending,now_monotonic=130).action,'recover')

    def test_missing_data_resets_timer_and_a_sample_gap_restarts_both_phases(self):
        for active,value,phase in ((False,30,'trigger'),(True,25,'recovery')):
            pending = PendingThreshold(0,1000,phase)
            self.assertEqual(self.decide(active=active,value=value,pending=pending,sample_fresh=False).action,'reset')
            resumed = self.decide(active=active,value=value,pending=pending,sample_gap=True)
            self.assertEqual(resumed.action,'hold')
            self.assertEqual(resumed.pending.since_monotonic,100)
        self.assertFalse(sampling_interrupted(None,100,20))
        self.assertFalse(sampling_interrupted(100,119.9,20))
        self.assertTrue(sampling_interrupted(100,120,20))


@unittest.skipUnless(shutil.which('node'),'Node is required for the actual dashboard behavior tests')
class DashboardBehaviorTests(unittest.TestCase):
    def test_history_selection_and_export_behavior(self):
        result = subprocess.run([shutil.which('node'),'--test',str(Path(__file__).with_name('test_dashboard.js'))],
                                capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
