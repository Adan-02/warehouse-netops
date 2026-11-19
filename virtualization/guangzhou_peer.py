"""Generate an owned, reversible Guangzhou WireGuard endpoint bundle.

Generation is local only. Nothing here connects to a server or invokes SSH.
The resulting directory contains a private key and must not be published.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

INTERFACE = "wg-warehouse"
SERVER_ADDRESS = "10.89.0.1/32"
ROUTER_ADDRESS = "10.89.0.2/32"
SERVICE_ADDRESS = "192.168.30.10/32"
PORT = 51830
MARKER = "warehouse-netops-guangzhou-v1"


def read_key(path: Path, label: str) -> str:
    value = path.read_text(encoding="ascii").strip()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid {label} key file") from error
    if len(decoded) != 32 or not any(decoded) or base64.b64encode(decoded).decode() != value:
        raise ValueError(f"Invalid {label} key file")
    return value


FIREWALL_SCRIPT = (Path(__file__).with_name('templates')/'guangzhou'/'firewall.sh.in').read_text(encoding='utf-8')


INSTALL_SCRIPT = (Path(__file__).with_name('templates')/'guangzhou'/'install.sh.in').read_text(encoding='utf-8')


def generate_bundle(
    output_dir: Path,
    server_private_key_file: Path,
    router_public_key_file: Path,
    *,
    wan_interface: str = "eth0",
    bundle_id: str | None = None,
) -> dict[str, Path]:
    """Create a fresh private bundle; never execute or connect to any host."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}", wan_interface) or wan_interface in {"lo", INTERFACE}:
        raise ValueError("Invalid WAN interface name")
    identifier = bundle_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("bundle_id must be 32 lowercase hex characters")
    private = read_key(Path(server_private_key_file), "server private")
    peer = read_key(Path(router_public_key_file), "router public")
    output = Path(output_dir)
    if output.is_symlink() or (output.exists() and any(output.iterdir())):
        raise ValueError("Output directory must be new or empty and must not be a symlink")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    owner = f"{MARKER}:{identifier}"
    replacement = lambda text: text.replace("@OWNER@", owner).replace("@WAN_INTERFACE@", wan_interface)
    header = f"# {owner}\n"
    contents = {
        ".project-marker": owner + "\n",
        "wg-warehouse.conf": header + f"""[Interface]
Address = {SERVER_ADDRESS}
ListenPort = {PORT}
PrivateKey = {private}

[Peer]
PublicKey = {peer}
AllowedIPs = {ROUTER_ADDRESS}, {SERVICE_ADDRESS}
""",
        "firewall.sh": replacement(FIREWALL_SCRIPT),
        "install.sh": replacement(INSTALL_SCRIPT),
        "warehouse-wg-firewall.service": header + """[Unit]
Description=Warehouse isolated WireGuard input rules
After=network-online.target qq-stack-firewall.service
Before=wg-quick@wg-warehouse.service
PartOf=qq-stack-firewall.service
ReloadPropagatedFrom=qq-stack-firewall.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/lib/warehouse-netops-guangzhou/firewall.sh up
ExecReload=/usr/local/lib/warehouse-netops-guangzhou/firewall.sh up
ExecStop=/usr/local/lib/warehouse-netops-guangzhou/firewall.sh down

[Install]
WantedBy=multi-user.target
""",
        "warehouse.conf": header + """[Unit]
Requires=warehouse-wg-firewall.service
After=warehouse-wg-firewall.service
PartOf=warehouse-wg-firewall.service

[Service]
ExecStart=
ExecStart=/usr/bin/wg-quick up /var/lib/warehouse-netops-guangzhou/private/wg-warehouse.conf
ExecStop=
ExecStop=/usr/bin/wg-quick down /var/lib/warehouse-netops-guangzhou/private/wg-warehouse.conf
""",
        "warehouse-wg-reconcile.service": header + """[Unit]
Description=Reconcile warehouse-owned WireGuard input hooks
After=qq-stack-firewall.service warehouse-wg-firewall.service
ConditionPathExists=/var/lib/warehouse-netops-guangzhou/.installed

[Service]
Type=oneshot
ExecStart=/usr/local/lib/warehouse-netops-guangzhou/firewall.sh reconcile
""",
        "warehouse-wg-reconcile.timer": header + """[Unit]
Description=Recheck warehouse WireGuard hooks after independent firewall changes
PartOf=warehouse-wg-firewall.service

[Timer]
OnBootSec=30s
OnUnitActiveSec=15s
Unit=warehouse-wg-reconcile.service

[Install]
WantedBy=timers.target
""",
    }
    paths: dict[str, Path] = {}
    for name, text in contents.items():
        path = output / name
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        path.chmod(0o700 if name.endswith(".sh") else 0o600)
        paths[name] = path
    manifest = {
        "project": MARKER,
        "bundle_id": identifier,
        "interface": INTERFACE,
        "server_address": SERVER_ADDRESS,
        "peer_allowed_ips": [ROUTER_ADDRESS, SERVICE_ADDRESS],
        "listen_port": PORT,
        "wan_interface": wan_interface,
        "scope": "Guangzhou only; no US access; no NAT/FORWARD/sysctl/default route changes",
        "rollback_keeps_private_backups": True,
        "security_group": "Separate single UDP 51830 ingress rule is needed; this bundle does not edit cloud-provider rules",
    }
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o600)
    paths[path.name] = path
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n" for name, path in sorted(paths.items()))
    path = output / "checksums.txt"
    path.write_text(checksums, encoding="ascii", newline="\n")
    path.chmod(0o600)
    paths[path.name] = path
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a private Guangzhou-only WireGuard deployment bundle")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--server-private-key-file", type=Path, required=True)
    parser.add_argument("--router-public-key-file", type=Path, required=True)
    parser.add_argument("--wan-interface", default="eth0")
    args = parser.parse_args()
    try:
        paths = generate_bundle(args.output_dir, args.server_private_key_file, args.router_public_key_file,
                                wan_interface=args.wan_interface)
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(1, f"Bundle generation failed: {type(error).__name__}; verify input files and output directory.\n")
    print(json.dumps({"output_dir": str(args.output_dir.resolve()), "files": sorted(paths), "contains_private_key": True}))


if __name__ == "__main__":
    main()
