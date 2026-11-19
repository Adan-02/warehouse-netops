"""Run from each real network zone; only the designated lab server is probed."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import ipaddress
import json
from pathlib import Path
import socket
import sys
import time

POLICY = {
    "office": {8080: True, 1883: False, 22: False},
    "iot": {8080: False, 1883: True, 22: False},
    "vpn": {8080: True, 1883: False, 22: True},
    "wan": {8080: False, 1883: False, 22: False},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="从真实分区检查仓库主机的指定 TCP 端口")
    parser.add_argument("--role", choices=POLICY, required=True)
    parser.add_argument("--host", default="192.168.30.10")
    parser.add_argument("--source-ip", required=True, help="测试终端当前分区的 IPv4 地址")
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    source = str(ipaddress.IPv4Address(arguments.source_ip))
    host = str(ipaddress.IPv4Address(arguments.host))
    if arguments.timeout <= 0 or arguments.timeout > 30:
        parser.error("timeout 必须在 0 到 30 秒之间")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as source_check:
            source_check.bind((source, 0))
    except OSError:
        parser.error("测试终端没有这个源 IP，先连接指定分区并核对地址")
    results = []
    for port, expected in POLICY[arguments.role].items():
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=arguments.timeout, source_address=(source, 0)):
                connected, detail, inconclusive = True, "connected", False
        except OSError as error:
            connected, detail = False, f"{type(error).__name__}: {error}"
            inconclusive = error.errno in {
                errno.EADDRNOTAVAIL, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES,
                10049, 10051, 10065, 10013,
            }
        # A failed connection alone cannot prove which firewall rejected it.
        results.append({
            "port": port,
            "expected_connect": expected,
            "connected": connected,
            "matches_expected": connected == expected and not inconclusive,
            "inconclusive": inconclusive,
            "needs_firewall_evidence": not expected,
            "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            "detail": detail,
        })
    report = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "role": arguments.role,
        "source_ip": source,
        "destination": host,
        "results": results,
        "interpretation": "拒绝项需要结合路由、正常服务监听状态和路由器计数器验证。TCP 结果不证明 MQTT 主题权限。",
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if all(row["matches_expected"] for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())
