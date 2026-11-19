"""Publish explicit SIMULATED readings over the IoT network, without Docker.

Install tools/publisher-requirements.txt in a virtual environment, then run:
  python tools/publish_sensor.py --credentials-file /etc/warehouse/device.env \
      --confirm-demo --source-ip 192.168.20.100 --count 12

The operator must enable DEMO_MODE=true on the dashboard first. This publisher
never queries TCP 8080: the IoT zone is intentionally forbidden to access it.
The private env file needs MQTT_DEVICE_PASSWORD; MQTT_DEVICE_USERNAME, if set,
must be cabinet-01. Use --count 0 (the default) to run until interrupted.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
import uuid

DEVICE_ID = "cabinet-01"
TOPIC = "warehouse/cabinet-01/telemetry"
MAX_SEQUENCE = 2**53 - 1


class PublisherError(Exception):
    """A deliberately credential-free message safe to show to an operator."""


def make_payload(sequence: int, boot_id: str | None = None, scenario: str = 'normal') -> str:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= MAX_SEQUENCE:
        raise PublisherError("Sequence must be a positive JSON-safe integer.")
    index = sequence - 1
    if boot_id is not None and not re.fullmatch(r'[0-9a-f]{32}', boot_id):
        raise PublisherError('boot_id must contain 32 lowercase hexadecimal characters.')
    if scenario not in ('normal','high-temperature','high-humidity','recovery'):
        raise PublisherError('Unknown simulated scenario.')
    reading = {
            "device_id": DEVICE_ID,
            "seq": sequence,
            "temperature_c": round(24.0 + 1.2 * math.sin(index / 3), 2),
            "humidity_pct": round(48.0 + 7.0 * math.cos(index / 4), 2),
        }
    if boot_id is not None:
        reading['boot_id'] = boot_id
    if scenario == 'high-temperature': reading['temperature_c'] = 32.0
    if scenario == 'high-humidity': reading['humidity_pct'] = 82.0
    if scenario == 'recovery':
        reading['temperature_c'], reading['humidity_pct'] = 24.0, 48.0
    return json.dumps(
        reading,
        separators=(",", ":"),
        allow_nan=False,
    )


def read_credentials(path: Path) -> tuple[str, str]:
    """Read a small dotenv file as data; never source/execute it or log values."""
    try:
        if path.stat().st_size > 64 * 1024:
            raise PublisherError("Credential file is too large.")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise PublisherError("Cannot read the private UTF-8 credential file.") from None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", stripped)
        if not match:
            raise PublisherError("Credential file must contain KEY=value lines.")
        key, value = match.groups()
        value = value.strip()
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise PublisherError("Credential file contains an unmatched quote.")
            value = value[1:-1]
        if key in fields:
            raise PublisherError("Credential file contains a duplicate key.")
        fields[key] = value
    username = fields.get("MQTT_DEVICE_USERNAME", DEVICE_ID)
    password = fields.get("MQTT_DEVICE_PASSWORD", "")
    if username != DEVICE_ID:
        raise PublisherError("MQTT_DEVICE_USERNAME must be cabinet-01.")
    if not password or "\x00" in password:
        raise PublisherError("Credential file needs a nonempty MQTT_DEVICE_PASSWORD.")
    if "DEMO_MODE" in fields and fields["DEMO_MODE"].lower() not in ("true", "1"):
        raise PublisherError("Credential file indicates DEMO_MODE is disabled.")
    return username, password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="显式模拟温湿度，经真实 TCP 从 IoT 网发布；不依赖 Docker")
    parser.add_argument("--host", default="192.168.30.10")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--source-ip", help="绑定本机真实的 IoT IPv4 地址")
    parser.add_argument("--credentials-file", type=Path, required=True, help="私有 KEY=value 文件；Linux 上设为 0600")
    parser.add_argument("--confirm-demo", action="store_true", help="确认服务端已开启 DEMO_MODE=true")
    parser.add_argument("--count", type=int, default=0, help="消息数；0 为持续运行，Ctrl+C 停止")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--publish-timeout", type=float, default=10.0)
    parser.add_argument('--scenario', choices=('normal','high-temperature','high-humidity','recovery'), default='normal', help='显式模拟场景；用于告警与恢复演示')
    return parser


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.confirm_demo:
        parser.error("必须用 --confirm-demo 确认服务端 DEMO_MODE=true；本程序不会访问看板端口")
    if not args.host.strip() or any(c.isspace() for c in args.host):
        parser.error("host 必须是非空主机名或地址")
    if not 1 <= args.port <= 65535:
        parser.error("port 必须为 1–65535")
    if not 0 <= args.count <= MAX_SEQUENCE:
        parser.error("count 必须为 0 或 JSON 安全范围内的正整数")
    for option in ("interval", "connect_timeout", "publish_timeout"):
        value = getattr(args, option)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{option.replace('_', '-')} 必须为有限正数")
    if args.source_ip:
        try:
            args.source_ip = str(ipaddress.IPv4Address(args.source_ip))
        except ipaddress.AddressValueError:
            parser.error("source-ip 必须是本机真实的 IPv4 地址")
    return args


def run_publisher(args: argparse.Namespace, username: str, password: str, mqtt_module=None) -> None:
    if mqtt_module is None:
        try:
            import paho.mqtt.client as mqtt_module
        except ImportError:
            raise PublisherError("Install tools/publisher-requirements.txt in a virtual environment first.") from None

    connected = threading.Event()
    disconnected = threading.Event()
    connect_accepted: list[bool] = []
    acknowledgements: dict[int, bool] = {}
    acknowledgement_lock = threading.Lock()
    client = mqtt_module.Client(
        mqtt_module.CallbackAPIVersion.VERSION2,
        client_id="warehouse-sim-" + uuid.uuid4().hex[:12],
        protocol=mqtt_module.MQTTv5,
        reconnect_on_failure=False,
    )
    client.username_pw_set(username, password)

    def on_connect(connection, userdata, flags, reason_code, properties):
        connect_accepted.append(not reason_code.is_failure)
        connected.set()

    def on_disconnect(connection, userdata, flags, reason_code, properties):
        disconnected.set()

    def on_publish(connection, userdata, mid, reason_code, properties):
        with acknowledgement_lock:
            acknowledgements[mid] = not reason_code.is_failure

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish = on_publish
    loop_started = False
    try:
        # connect() performs TCP synchronously; the loop processes CONNACK/PUBACK.
        client.connect_timeout = args.connect_timeout
        result = client.connect(args.host, args.port, keepalive=30, bind_address=args.source_ip or "")
        if result != mqtt_module.MQTT_ERR_SUCCESS:
            raise PublisherError("MQTT connection could not be started.")
        client.loop_start()
        loop_started = True
        if not connected.wait(args.connect_timeout):
            raise PublisherError("Timed out waiting for MQTT CONNACK.")
        if not connect_accepted or not connect_accepted[0]:
            raise PublisherError("MQTT connection/authentication was rejected.")
        sequence = 1
        boot_id = uuid.uuid4().hex
        while args.count == 0 or sequence <= args.count:
            if disconnected.is_set():
                raise PublisherError("MQTT disconnected; no further samples were published.")
            payload = make_payload(sequence, boot_id, getattr(args,'scenario','normal'))
            info = client.publish(TOPIC, payload, qos=1, retain=False)
            if info.rc != mqtt_module.MQTT_ERR_SUCCESS:
                raise PublisherError("MQTT did not queue the sample.")
            info.wait_for_publish(timeout=args.publish_timeout)
            if not info.is_published():
                raise PublisherError("Timed out waiting for MQTT PUBACK.")
            with acknowledgement_lock:
                accepted = acknowledgements.pop(info.mid, False)
            if not accepted:
                raise PublisherError("MQTT broker rejected the sample; it was not accepted.")
            # A successful queue operation alone is not a successful publish.
            print("SIMULATED PUBACK accepted " + payload, flush=True)
            sequence += 1
            if args.count == 0 or sequence <= args.count:
                if disconnected.wait(args.interval):
                    raise PublisherError("MQTT disconnected; no further samples were published.")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        if loop_started:
            client.loop_stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        username, password = read_credentials(args.credentials_file)
        print("SIMULATED source. Confirm dashboard DEMO_MODE=true. No HTTP/API access is attempted.", flush=True)
        run_publisher(args, username, password)
        return 0
    except KeyboardInterrupt:
        print("SIMULATED publisher stopped.", flush=True)
        return 130
    except PublisherError as error:
        print(f"Publisher failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Exceptions from network/auth libraries may embed endpoints or values.
        print(f"Publisher failed ({type(error).__name__}); check IoT addressing, policy and broker status.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
