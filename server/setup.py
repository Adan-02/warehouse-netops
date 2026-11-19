"""Create project-local broker credentials without writing secrets to source files."""

import argparse
import ipaddress
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description="初始化仓库演示服务的本地配置")
    parser.add_argument("--bind-ip", default=None, help="只绑定这一个 IPv4 地址，默认 127.0.0.1")
    parser.add_argument("--demo", action=argparse.BooleanOptionalAction, default=None,
                        help="标明数据来自模拟发布器，--no-demo 切回实物模式")
    args = parser.parse_args()
    path = Path(__file__).resolve().parent / ".env"
    bind_ip = args.bind_ip or "127.0.0.1"
    try:
        address = ipaddress.IPv4Address(bind_ip)
        if address.is_unspecified or address.is_multicast:
            parser.error("请使用具体的主机接口 IPv4 地址")
    except ipaddress.AddressValueError:
        parser.error("--bind-ip 需要一个合法 IPv4 地址")
    if path.exists():
        existing = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                        if line and not line.startswith("#") and "=" in line)
        required = ("SERVICE_BIND_IP", "MQTT_DEVICE_PASSWORD", "MQTT_VIEWER_PASSWORD")
        if not all(existing.get(key) for key in required):
            parser.error("现有 .env 不完整，请先备份并修正，初始化不会覆盖凭据")
        if args.bind_ip is not None:
            existing["SERVICE_BIND_IP"] = bind_ip
        if args.demo is not None:
            existing["DEMO_MODE"] = "true" if args.demo else "false"
        if args.bind_ip is not None or args.demo is not None:
            temporary = path.with_suffix(".env.tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write("".join(f"{key}={value}\n" for key, value in existing.items()))
            temporary.chmod(0o600)
            os.replace(temporary, path)
        print("已有 server/.env，保留现有凭据。重建容器后应用显式传入的地址或演示模式。")
        return
    device_password = secrets.token_urlsafe(24)
    viewer_password = secrets.token_urlsafe(24)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(f"SERVICE_BIND_IP={bind_ip}\n")
        stream.write(f"MQTT_DEVICE_PASSWORD={device_password}\n")
        stream.write(f"MQTT_VIEWER_PASSWORD={viewer_password}\n")
        stream.write(f"DEMO_MODE={'true' if args.demo else 'false'}\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print("已创建 server/.env，两个账号使用不同的随机密码。")
    print(f"服务绑定 {bind_ip}。ESP32 的设备密码从本地 .env 复制，保留此文件供下次启动使用。")


if __name__ == "__main__":
    main()
