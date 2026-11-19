"""Local Docker lab operations. Demo telemetry is always explicitly requested."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
PROJECT = "warehouse-netops"


def compose(*arguments: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "--project-name", PROJECT, "-f", str(SERVER / "compose.yaml"), *arguments],
        cwd=SERVER,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=capture,
    )


def status(base_url: str) -> dict:
    with urllib.request.urlopen(base_url.rstrip("/") + "/api/status", timeout=5) as response:
        return json.load(response)


def publish_demo(seq: int, temperature: float, humidity: float | None = None) -> None:
    reading = {"device_id": "cabinet-01", "seq": seq, "temperature_c": temperature}
    if humidity is not None:
        reading["humidity_pct"] = humidity
    payload = json.dumps(reading, separators=(",", ":"))
    # The broker reads its own credential environment. Passwords never enter command output.
    command = (
        'exec mosquitto_pub -h 127.0.0.1 -p 1883 -u cabinet-01 '
        '-P "$MQTT_DEVICE_PASSWORD" -t warehouse/cabinet-01/telemetry '
        '-q 1 -V mqttv5 -m "$1"'
    )
    compose("exec", "-T", "broker", "sh", "-c", command, "demo", payload, capture=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="仓库网络运维实验包")
    sub = parser.add_subparsers(dest="action", required=True)
    initialize = sub.add_parser("init", help="生成本机密钥和服务配置")
    initialize.add_argument("--bind-ip", default=None)
    initialize.add_argument("--demo", action="store_true", help="看板明确标注模拟数据")
    sub.add_parser("up", help="构建并启动容器")
    sub.add_parser("down", help="停止容器，保留历史卷")
    sub.add_parser("ps", help="查看容器状态")
    check = sub.add_parser("status", help="读取 API 状态")
    check.add_argument("--url", default="http://127.0.0.1:8080")
    demo = sub.add_parser("demo", help="明确向本地 Broker 发送模拟温湿度，Ctrl+C 停止")
    demo.add_argument("--count", type=int, default=12)
    demo.add_argument("--interval", type=float, default=5)
    arguments = parser.parse_args()
    if arguments.action == "init":
        setup_arguments = []
        if arguments.bind_ip is not None:
            setup_arguments.extend(["--bind-ip", arguments.bind_ip])
        if arguments.demo:
            setup_arguments.append("--demo")
        subprocess.run(
            [sys.executable, str(SERVER / "setup.py"), *setup_arguments],
            cwd=SERVER,
            check=True,
        )
    elif arguments.action == "up":
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "120")
    elif arguments.action == "down":
        compose("down")
    elif arguments.action == "ps":
        compose("ps")
    elif arguments.action == "status":
        print(json.dumps(status(arguments.url), ensure_ascii=False, indent=2))
    elif arguments.action == "demo":
        if arguments.count < 1 or arguments.interval < 0 or not math.isfinite(arguments.interval):
            parser.error("count 必须为正整数，interval 必须是有限非负数")
        print("正在发送明确的模拟温湿度。验证真实传感器前请停止本命令。")
        for index in range(arguments.count):
            temperature = round(24.0 + 1.2 * math.sin(index / 3), 2)
            humidity = round(48.0 + 7.0 * math.cos(index / 4), 2)
            publish_demo(index + 1, temperature, humidity)
            print(f"模拟 #{index + 1}  {temperature:.2f} °C  {humidity:.2f} %RH")
            if index + 1 < arguments.count:
                time.sleep(arguments.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("模拟发送已停止。")
    except (subprocess.CalledProcessError, OSError, urllib.error.URLError) as error:
        print(f"实验命令失败，检查 Docker 状态和服务日志。{type(error).__name__}", file=sys.stderr)
        sys.exit(1)
