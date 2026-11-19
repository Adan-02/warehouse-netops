import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.state import TelemetryStore, parse_reading
from app.main import create_app

MISSING = object()


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = TelemetryStore(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def payload(self, seq=1, temperature=27.5, humidity=45.0):
        value = dict(device_id="cabinet-01", seq=seq, temperature_c=temperature)
        if humidity is not MISSING:
            value["humidity_pct"] = humidity
        return json.dumps(value).encode()

    def test_initial_online_stale_and_broker_fault_are_distinct(self):
        self.assertEqual(self.store.status()["status"], "broker_disconnected")
        self.store.set_broker_connected(True)
        self.assertEqual(self.store.status()["status"], "awaiting_data")
        self.store.ingest(self.payload())
        self.assertEqual(self.store.status()["status"], "online")
        self.assertEqual(self.store.status()["humidity_pct"], 45.0)
        self.now += 20
        self.assertEqual(self.store.status()["status"], "stale")
        self.store.set_broker_connected(False)
        self.assertEqual(self.store.status()["status"], "broker_disconnected")
        self.assertEqual(self.store.status()["data_status"], "stale")

    def test_bad_payload_and_retained_data_never_refresh_liveness(self):
        self.store.ingest(self.payload())
        self.now += 21
        values = [b"null", b"[]", b"\xff", b"{", self.payload(seq=True), self.payload(seq=0),
                  self.payload(temperature=True), self.payload(temperature=float("nan")),
                  self.payload(temperature=float("inf")), self.payload(temperature=10**400),
                  self.payload(humidity=True), self.payload(humidity=None),
                  self.payload(humidity="45"), self.payload(humidity=float("nan")),
                  self.payload(humidity=float("inf")), self.payload(humidity=10**400),
                  self.payload(humidity=-0.1), self.payload(humidity=100.1), b"x" * 4097]
        for payload in values:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.store.ingest(payload)
        with self.assertRaises(ValueError):
            self.store.ingest(self.payload(seq=2), retained=True)
        self.assertEqual(self.store.status()["seq"], 1)
        self.assertEqual(self.store.status()["data_status"], "stale")
        self.assertEqual(self.store.status()["last_seen_at"], "1970-01-01T00:16:40+00:00")
        self.assertEqual(self.store.status()["humidity_pct"], 45.0)

    def test_readings_survive_restart_and_sequence_reset(self):
        self.store.ingest(self.payload(seq=99))
        self.store.close()
        self.now += 25
        self.store = TelemetryStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.status()["seq"], 99)
        self.assertEqual(self.store.status()["humidity_pct"], 45.0)
        self.assertEqual(self.store.status()["data_status"], "stale")
        self.store.ingest(self.payload(seq=1))
        self.assertEqual([item["seq"] for item in self.store.history()], [99, 1])

    def test_humidity_boundaries_are_valid_and_returned_by_apis(self):
        for index, humidity in enumerate((0, 100), start=1):
            self.store.ingest(self.payload(seq=index, humidity=humidity))
        self.assertEqual([item["humidity_pct"] for item in self.store.history()], [0.0, 100.0])
        client = create_app(self.store).test_client()
        self.assertEqual(client.get("/api/status").json["humidity_pct"], 100.0)
        self.assertEqual(client.get("/api/history").json["readings"][0]["humidity_pct"], 0.0)

    def test_legacy_message_has_missing_humidity_instead_of_reusing_previous_value(self):
        self.store.ingest(self.payload())
        self.now += 5
        self.store.ingest(self.payload(seq=2, humidity=MISSING))
        self.assertIsNone(self.store.status()["humidity_pct"])
        self.assertEqual([item["humidity_pct"] for item in self.store.history()], [45.0, None])
        self.assertEqual(self.store.status()["age_seconds"], 0)
        page = create_app(self.store).test_client().get("/").get_data(as_text=True)
        self.assertIn("未上报", page)
        self.assertIn("humidity-chart", page)

    def test_old_schema_migration_preserves_record_and_is_idempotent(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as database:
            database.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, seq INTEGER NOT NULL, "
                             "temperature_c REAL NOT NULL, received_at REAL NOT NULL)")
            database.execute("INSERT INTO readings(seq, temperature_c, received_at) VALUES (9, 22.5, 900)")
        for _ in range(2):
            migrated = TelemetryStore(legacy_path, clock=lambda: self.now)
            try:
                old = migrated.history()[0]
                self.assertEqual(old, dict(seq=9, temperature_c=22.5, humidity_pct=None,
                                           received_at="1970-01-01T00:15:00+00:00"))
                self.assertEqual(migrated.status()["data_status"], "stale")
                columns = [row[1] for row in migrated.db.execute("PRAGMA table_info(readings)")]
                self.assertEqual(columns.count("humidity_pct"), 1)
            finally:
                migrated.close()
        migrated = TelemetryStore(legacy_path, clock=lambda: self.now)
        try:
            migrated.ingest(self.payload(seq=10, humidity=60))
            self.assertEqual(len(migrated.history()), 2)
            self.assertEqual(migrated.status()["humidity_pct"], 60.0)
        finally:
            migrated.close()

    def test_http_routes_and_limit_validation(self):
        client = create_app(self.store).test_client()
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/api/status").json["device_id"], "cabinet-01")
        for limit in ["0", "241", "x"]:
            self.assertEqual(client.get("/api/history?limit=" + limit).status_code, 400)
        self.assertEqual(client.post("/api/status").status_code, 405)
        self.assertIn("等待数据", client.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
