"""Validate readings and retain broker/telemetry health as separate facts."""

import json
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DEVICE_ID = "cabinet-01"
TOPIC = "warehouse/cabinet-01/telemetry"


def iso_timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def finite_number(value, field):
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be finite")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{field} must be finite") from error
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def parse_reading(payload):
    """Reject malformed input and bool-as-int values before database writes."""
    if len(payload) > 4096:
        raise ValueError("payload exceeds 4096 bytes")
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(value, dict) or value.get("device_id") != DEVICE_ID:
        raise ValueError("unexpected device_id")
    seq = value.get("seq")
    if type(seq) is not int or not 1 <= seq <= 2**53 - 1:
        raise ValueError("seq must be a positive JSON-safe integer")
    temperature = finite_number(value.get("temperature_c"), "temperature_c")
    humidity = None
    if "humidity_pct" in value:
        humidity = finite_number(value["humidity_pct"], "humidity_pct")
        if not 0 <= humidity <= 100:
            raise ValueError("humidity_pct must be between 0 and 100")
    return seq, temperature, humidity


class TelemetryStore:
    def __init__(self, db_path, offline_after_seconds=20, clock=time.time):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        # Serialize schema inspection and alteration so repeated startup is safe.
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY, "
                "seq INTEGER NOT NULL, temperature_c REAL NOT NULL, received_at REAL NOT NULL, "
                "humidity_pct REAL)"
            )
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(readings)")}
            if "humidity_pct" not in columns:
                self.db.execute("ALTER TABLE readings ADD COLUMN humidity_pct REAL")
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise
        self.lock = threading.RLock()
        self.broker_connected = False
        self.offline_after_seconds = offline_after_seconds
        self.clock = clock

    def set_broker_connected(self, connected):
        with self.lock:
            self.broker_connected = bool(connected)

    def ingest(self, payload, retained=False):
        # A retained historical packet cannot prove that a device is alive now.
        if retained:
            raise ValueError("retained telemetry is not a live heartbeat")
        seq, temperature, humidity = parse_reading(payload)
        with self.lock:
            self.db.execute(
                "INSERT INTO readings(seq, temperature_c, humidity_pct, received_at) VALUES (?, ?, ?, ?)",
                (seq, temperature, humidity, self.clock()),
            )
            self.db.commit()

    def status(self):
        with self.lock:
            row = self.db.execute(
                "SELECT seq, temperature_c, humidity_pct, received_at FROM readings ORDER BY id DESC LIMIT 1"
            ).fetchone()
            now = self.clock()
            age = max(0, now - row[3]) if row else None
            data_status = "awaiting_data" if row is None else (
                "online" if age < self.offline_after_seconds else "stale"
            )
            return {
                "device_id": DEVICE_ID,
                "status": data_status if self.broker_connected else "broker_disconnected",
                "data_status": data_status,
                "broker_connected": self.broker_connected,
                "seq": row[0] if row else None,
                "temperature_c": row[1] if row else None,
                "humidity_pct": row[2] if row else None,
                "last_seen_at": iso_timestamp(row[3]) if row else None,
                "age_seconds": round(age, 1) if age is not None else None,
                "offline_after_seconds": self.offline_after_seconds,
                "observed_at": iso_timestamp(now),
            }

    def history(self, limit=120):
        with self.lock:
            rows = self.db.execute(
                "SELECT seq, temperature_c, humidity_pct, received_at FROM readings ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"seq": seq, "temperature_c": temperature, "humidity_pct": humidity,
             "received_at": iso_timestamp(received)}
            for seq, temperature, humidity, received in reversed(rows)
        ]

    def close(self):
        with self.lock:
            self.db.close()
