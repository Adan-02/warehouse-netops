"""Pure tests: no running broker, Docker, cloud, or host networking changes."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest

SPEC = importlib.util.spec_from_file_location("publish_sensor", Path(__file__).parents[1] / "publish_sensor.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class PublisherTests(unittest.TestCase):
    def credentials(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.env"
            path.write_text(text, encoding="utf-8")
            return publisher.read_credentials(path)

    def arguments(self, *extra):
        return publisher.parse_arguments(["--credentials-file", "private.env", "--confirm-demo", *extra])

    def test_contract_and_finite_sensor_ranges(self):
        for sequence in (1, 2, 120, 5000, publisher.MAX_SEQUENCE):
            item = json.loads(publisher.make_payload(sequence))
            self.assertEqual(item["device_id"], "cabinet-01")
            self.assertEqual(item["seq"], sequence)
            self.assertTrue(-40 <= item["temperature_c"] <= 80)
            self.assertTrue(0 <= item["humidity_pct"] <= 100)
            self.assertEqual(set(item), {"device_id", "seq", "temperature_c", "humidity_pct"})

    def test_invalid_sequence(self):
        for value in (0, -1, True, 1.0, publisher.MAX_SEQUENCE + 1):
            with self.assertRaises(publisher.PublisherError):
                publisher.make_payload(value)

    def test_startup_identity_and_explicit_alarm_scenarios(self):
        boot = 'a' * 32
        first = json.loads(publisher.make_payload(1,boot,'high-temperature'))
        second = json.loads(publisher.make_payload(2,boot,'recovery'))
        self.assertEqual(first['boot_id'],second['boot_id'])
        self.assertEqual(first['temperature_c'],32.0)
        self.assertEqual(second['humidity_pct'],48.0)
        self.assertEqual(json.loads(publisher.make_payload(1,boot,'high-humidity'))['humidity_pct'],82.0)
        for value in ('x'*32,'a'*31,'A'*32):
            with self.assertRaises(publisher.PublisherError): publisher.make_payload(1,value)

    def test_private_credentials_as_data(self):
        self.assertEqual(self.credentials("# local\nexport MQTT_DEVICE_USERNAME=cabinet-01\nMQTT_DEVICE_PASSWORD='$(do-not-execute)#private'\nDEMO_MODE=true\n"), ("cabinet-01", "$(do-not-execute)#private"))
        self.assertEqual(self.credentials('MQTT_DEVICE_PASSWORD="a=b#c"\n'), ("cabinet-01", "a=b#c"))

    def test_invalid_credentials_never_echo_values(self):
        secret = "do-not-leak-this"
        for text in (f"MQTT_DEVICE_USERNAME={secret}\nMQTT_DEVICE_PASSWORD=x", f"MQTT_DEVICE_PASSWORD='{secret}", f"MQTT_DEVICE_PASSWORD={secret}\nMQTT_DEVICE_PASSWORD=second", f"MQTT_DEVICE_PASSWORD={secret}\ninvalid line", "MQTT_DEVICE_PASSWORD=", f"MQTT_DEVICE_PASSWORD={secret}\nDEMO_MODE=false"):
            with self.assertRaises(publisher.PublisherError) as failure:
                self.credentials(text)
            self.assertNotIn(secret, str(failure.exception))

    def test_missing_private_file_is_sanitized(self):
        with self.assertRaises(publisher.PublisherError) as failure:
            publisher.read_credentials(Path("does-not-exist-private-value"))
        self.assertNotIn("does-not-exist-private-value", str(failure.exception))

    def test_defaults_and_source_binding(self):
        args = self.arguments("--source-ip", "192.168.20.100", "--count", "12")
        self.assertEqual((args.host, args.port, args.interval, args.count), ("192.168.30.10", 1883, 5, 12))
        self.assertEqual(args.source_ip, "192.168.20.100")
        self.assertEqual(self.arguments().count, 0)

    def test_invalid_parameters(self):
        invalid = [("--port", "0"), ("--port", "65536"), ("--count", "-1"), ("--interval", "nan"), ("--interval", "inf"), ("--interval", "0"), ("--source-ip", "::1"), ("--connect-timeout", "0"), ("--publish-timeout", "nan")]
        for arguments in invalid:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.arguments(*arguments)

    def test_demo_acknowledgement_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            publisher.parse_arguments(["--credentials-file", "private.env"])

    def fake_mqtt(self, *, publish_denied=False, connect_denied=False, unacknowledged=False):
        owner = self
        reason = lambda denied: types.SimpleNamespace(is_failure=denied)

        class Client:
            def __init__(self, *args, **kwargs):
                self.published = []
                owner.client = self
                owner.assertFalse(kwargs["reconnect_on_failure"])

            def username_pw_set(self, username, password):
                pass

            def connect(self, host, port, keepalive, bind_address):
                self.bind_address = bind_address
                return 0

            def loop_start(self):
                self.on_connect(self, None, None, reason(connect_denied), None)

            def publish(self, topic, payload, qos, retain):
                owner.assertEqual((topic, qos, retain), (publisher.TOPIC, 1, False))
                self.published.append(payload)
                mid = len(self.published)
                if not unacknowledged:
                    self.on_publish(self, None, mid, reason(publish_denied), None)
                return types.SimpleNamespace(rc=0, mid=mid, wait_for_publish=lambda timeout: None, is_published=lambda: not unacknowledged)

            def disconnect(self):
                pass

            def loop_stop(self):
                pass

        return types.SimpleNamespace(Client=Client, CallbackAPIVersion=types.SimpleNamespace(VERSION2=2), MQTTv5=5, MQTT_ERR_SUCCESS=0)

    def test_success_logged_only_after_accepted_puback(self):
        output = io.StringIO()
        with redirect_stdout(output):
            publisher.run_publisher(self.arguments("--count", "1", "--source-ip", "192.168.20.100"), "cabinet-01", "hidden", self.fake_mqtt())
        self.assertIn("SIMULATED PUBACK accepted", output.getvalue())
        self.assertNotIn("hidden", output.getvalue())
        self.assertEqual(self.client.bind_address, "192.168.20.100")

    def test_denied_or_missing_ack_is_failure_without_success_log(self):
        for flags in ({"publish_denied": True}, {"connect_denied": True}, {"unacknowledged": True}):
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(publisher.PublisherError):
                publisher.run_publisher(self.arguments("--count", "1"), "cabinet-01", "hidden", self.fake_mqtt(**flags))
            self.assertNotIn("PUBACK accepted", output.getvalue())


if __name__ == "__main__":
    unittest.main()
