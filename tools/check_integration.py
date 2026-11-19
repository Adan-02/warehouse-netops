"""Exercise the running local container stack and save software-only evidence.

Run with demo publishers paused. Broker/dashboard restarts belong to this lab only.
No hardware firewall, VPN, sensor or wireless claims are made by this test.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

from lab import PROJECT, ROOT, SERVER, compose, compose_arguments, publish_demo, status


def wait_for(base_url, predicate, description, timeout=45):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = status(base_url)
            if predicate(last):
                return last
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {description}. Last status={last}")


def mqtt_publish(role, topic, payload=""):
    # Credentials are expanded inside the broker container, never copied to Python or evidence.
    auth = {
        "device": '-u cabinet-01 -P "$MQTT_DEVICE_PASSWORD"',
        "viewer": '-u warehouse-dashboard -P "$MQTT_VIEWER_PASSWORD"',
        "wrong": '-u cabinet-01 -P deliberately-wrong-integration-password',
        "anonymous": "",
    }[role]
    command = f'exec mosquitto_pub -h 127.0.0.1 -p 1883 -V mqttv5 -d {auth} -t "$1" -q 1 -m "$2"'
    try:
        result = compose("exec", "-T", "broker", "sh", "-c", command,
                         "integration", topic, payload, capture=True)
        return result.returncode, result.stdout + result.stderr
    except subprocess.CalledProcessError as error:
        return error.returncode, (error.stdout or "") + (error.stderr or "")


def require_denied(role, topic):
    returncode, output = mqtt_publish(role, topic, "blocked-test-message")
    lowered = output.lower()
    denied = "not authori" in lowered or "not authorised" in lowered or "reason code 135" in lowered
    if not denied:
        raise AssertionError(f"Expected explicit MQTT authorization rejection for {role}/publish; exit={returncode}")


def main():
    parser = argparse.ArgumentParser(description="运行仓库网络运维实验包的软件集成验收")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence" / "software-integration.json")
    args = parser.parse_args()
    checks = []
    started = datetime.now(timezone.utc).isoformat()
    failure = None
    sequence_base = time.time_ns() // 1_000_000

    def check(name, callback):
        begin = time.monotonic()
        callback()
        checks.append({"name": name, "passed": True, "seconds": round(time.monotonic() - begin, 2)})
        print(f"PASS {name}", flush=True)

    try:
        wait_for(args.url, lambda value: value["broker_connected"], "MQTT subscription", timeout=120)

        def valid_publish():
            publish_demo(sequence_base + 1, 26.25, 48.5)
            value = wait_for(args.url, lambda value: value["seq"] == sequence_base + 1, "valid reading")
            assert value["temperature_c"] == 26.25 and value["data_status"] == "online"
            assert value["humidity_pct"] == 48.5

        check("authorized_device_publish_updates_api", valid_publish)
        check("anonymous_authentication_denied", lambda: require_denied("anonymous", "warehouse/cabinet-01/telemetry"))
        check("wrong_password_denied", lambda: require_denied("wrong", "warehouse/cabinet-01/telemetry"))
        check("device_other_topic_publish_denied", lambda: require_denied("device", "warehouse/cabinet-02/telemetry"))
        check("viewer_publish_denied", lambda: require_denied("viewer", "warehouse/cabinet-01/telemetry"))
        def device_cannot_read():
            # File ACLs authorize delivery. A successful SUBACK does not grant read access.
            probe = '''
import json, sys, threading
import paho.mqtt.client as mqtt
arguments = json.load(sys.stdin)
subscribed, published, received = threading.Event(), threading.Event(), threading.Event()
reasons = {}
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
c.username_pw_set("cabinet-01", arguments["password"])
def connected(client, userdata, flags, reason, properties):
    if reason.is_failure: return
    client.subscribe("warehouse/cabinet-01/telemetry", qos=1)
def suback(client, userdata, mid, values, properties):
    reasons["suback"] = values[0].value
    subscribed.set()
def puback(client, userdata, mid, reason, properties):
    reasons["puback"] = reason.value
    published.set()
def message(client, userdata, packet): received.set()
c.on_connect, c.on_subscribe, c.on_publish, c.on_message = connected, suback, puback, message
c.connect("broker", 1883, 15)
c.loop_start()
try:
    assert subscribed.wait(5), "No SUBACK"
    c.publish("warehouse/cabinet-01/telemetry", json.dumps(arguments["reading"]), qos=1)
    assert published.wait(5), "No PUBACK"
    assert reasons["puback"] == 0, "Device could not publish its own reading"
    reasons["read_delivery"] = received.wait(3)
    print(json.dumps(reasons), flush=True)
finally:
    c.disconnect()
    c.loop_stop()
'''
            # Transfer via captured stdout to child stdin, never through arguments or tool output.
            password = compose("exec", "-T", "broker", "sh", "-c",
                               'printf %s "$MQTT_DEVICE_PASSWORD"', capture=True).stdout
            result = subprocess.run(
                compose_arguments("exec", "-T", "dashboard", "python", "-c", probe),
                cwd=SERVER, input=json.dumps({"password": password, "reading": {
                    "device_id": "cabinet-01", "seq": sequence_base + 3,
                    "temperature_c": 26.5, "humidity_pct": 49.0,
                }}), capture_output=True, text=True, encoding="utf-8", timeout=20,
                check=True,
            )
            assert not json.loads(result.stdout)["read_delivery"], "Publish-only device received telemetry"
            wait_for(args.url, lambda value: value["seq"] == sequence_base + 3, "authorized viewer receipt")

        check("device_cannot_read_telemetry", device_cannot_read)

        def legacy_reading():
            publish_demo(sequence_base + 4, 24.25)
            value = wait_for(args.url, lambda value: value["seq"] == sequence_base + 4, "legacy temperature-only reading")
            assert value["temperature_c"] == 24.25 and value["humidity_pct"] is None
            with urllib.request.urlopen(args.url.rstrip("/") + "/api/history?limit=1", timeout=5) as response:
                history = json.load(response)
            assert history["readings"][0]["humidity_pct"] is None

        check("legacy_temperature_only_reading_has_unknown_humidity", legacy_reading)

        def humidity_boundaries():
            for offset, humidity in [(5, 0), (6, 100)]:
                sequence = sequence_base + offset
                publish_demo(sequence, 25.0, humidity)
                value = wait_for(args.url, lambda value: value["seq"] == sequence, f"humidity {humidity}")
                assert value["humidity_pct"] == humidity and value["data_status"] == "online"

        check("humidity_zero_and_hundred_percent_are_valid", humidity_boundaries)

        def invalid_humidity():
            before = status(args.url)
            for offset, humidity in enumerate([None, True, -0.1, 100.1, "50", float("nan"), float("inf")], start=20):
                payload = json.dumps({"device_id": "cabinet-01", "seq": sequence_base + offset,
                                      "temperature_c": 27, "humidity_pct": humidity})
                code, output = mqtt_publish("device", "warehouse/cabinet-01/telemetry", payload)
                assert code == 0 and "not authori" not in output.lower()
            time.sleep(1)
            after = status(args.url)
            assert after["last_seen_at"] == before["last_seen_at"]
            assert after["seq"] == before["seq"] and after["humidity_pct"] == before["humidity_pct"]

        check("invalid_humidity_rejects_whole_reading_without_refresh", invalid_humidity)

        def invalid_payload():
            before = status(args.url)
            code, _ = mqtt_publish("device", "warehouse/cabinet-01/telemetry", '{"device_id":"cabinet-01","seq":true,"temperature_c":27}')
            assert code == 0, "Authorized malformed message did not reach broker"
            time.sleep(1)
            after = status(args.url)
            assert after["last_seen_at"] == before["last_seen_at"] and after["seq"] == before["seq"]

        check("invalid_payload_does_not_refresh_freshness", invalid_payload)

        def stale():
            value = wait_for(args.url, lambda value: value["data_status"] == "stale", "20 second stale state", timeout=30)
            assert value["age_seconds"] >= value["offline_after_seconds"] == 20
            assert value["broker_connected"]

        check("twenty_second_telemetry_staleness", stale)

        def broker_fault():
            compose("stop", "broker", capture=True)
            value = wait_for(args.url, lambda value: value["status"] == "broker_disconnected", "broker outage")
            assert not value["broker_connected"]

        check("broker_outage_visible_while_http_stays_available", broker_fault)

        def recovery():
            compose("start", "broker", capture=True)
            wait_for(args.url, lambda value: value["broker_connected"], "broker recovery", timeout=60)
            publish_demo(sequence_base + 40, 26.75, 52.25)
            value = wait_for(args.url, lambda value: value["seq"] == sequence_base + 40, "post-recovery reading")
            assert value["status"] == "online" and value["humidity_pct"] == 52.25

        check("broker_and_telemetry_recover", recovery)

        def persistent_restart():
            before = status(args.url)
            compose("restart", "dashboard", capture=True)
            value = wait_for(args.url, lambda value: value["broker_connected"] and value["seq"] == sequence_base + 40,
                             "persistent reading after dashboard restart", timeout=60)
            assert value["last_seen_at"] == before["last_seen_at"]
            assert value["temperature_c"] == before["temperature_c"]
            assert value["humidity_pct"] == before["humidity_pct"]

        check("dashboard_restart_preserves_reading_and_receipt_time", persistent_restart)
    except (Exception, KeyboardInterrupt) as error:
        failure = str(error) if not isinstance(error, KeyboardInterrupt) else "Interrupted"
        # Avoid including subprocess argument strings in logs, even though these commands only reference env names.
        if isinstance(error, subprocess.CalledProcessError):
            failure = f"Docker Compose command failed (exit {error.returncode})"
        checks.append({"name": "execution_error", "passed": False, "error": failure})
        print(f"FAIL {failure}", file=sys.stderr, flush=True)
    finally:
        try:
            compose("up", "-d", "broker", "dashboard", capture=True)
        except Exception:
            checks.append({"name": "restore_services", "passed": False,
                           "error": "Could not restore broker/dashboard. Run python tools/lab.py up."})
            failure = failure or "Service restore failed"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        evidence = {
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "scope": "local Docker software integration with simulated temperature and humidity MQTT input",
            "hardware_validated": False,
            "hardware_pending": ["ESP32 real sensor", "OpenWrt isolation", "WireGuard access", "WAN disconnect"],
            "passed": failure is None,
            "checks": checks,
        }
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Evidence saved to {args.output}", flush=True)
    return 1 if failure else 0


if __name__ == "__main__":
    sys.exit(main())
