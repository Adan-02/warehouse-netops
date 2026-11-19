"""Generate NoCloud files for the three disposable Debian 13 warehouse VMs.

The output is data for genisoimage, not an installer for the Windows host.
JSON is used as the YAML-compatible serialization to avoid a PyYAML dependency.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import shlex
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from virtualization.lab_topology import ADDRESSES, GATEWAYS, STAGING_ADDRESSES, NAT_GATEWAY

ROLES = {
    role: {"staging_ip": STAGING_ADDRESSES[role], "final_ip": ADDRESSES[role] if role=='service' else None, "gateway": GATEWAYS[role]}
    for role in ('service', 'iot', 'office')
}

BASE_PACKAGES = [
    "ca-certificates", "curl", "iproute2", "iputils-ping", "openssh-server",
    "open-vm-tools", "python3", "python3-venv", "sudo", "systemd-resolved",
]
ROLE_PACKAGES = {
    "service": ["docker.io", "docker-cli", "docker-compose"],
    "iot": ["python3-pip", "python3-paho-mqtt"],
    "office": [
        "xfce4", "xfce4-terminal", "xserver-xorg-core", "xserver-xorg-input-libinput",
        "xserver-xorg-video-vmware", "xinit", "xauth", "lightdm", "firefox-esr",
        "dbus-x11", "fonts-dejavu-core", "fonts-noto-cjk",
    ],
}


def normalize_mac(value: str) -> str:
    mac = value.lower().replace("-", ":")
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise ValueError("MAC must contain six hexadecimal octets")
    if mac == "00:00:00:00:00:00" or int(mac[:2], 16) & 1:
        raise ValueError("MAC must be a nonzero unicast address")
    return mac


def read_public_key(path: Path) -> str:
    key = path.read_text(encoding="utf-8-sig").strip()
    parts = key.split()
    if len(key.splitlines()) != 1 or len(parts) < 2 or parts[0] not in {
        "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    }:
        raise ValueError("a single OpenSSH public key is required")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid public key encoding") from error
    if len(decoded) < 24:
        raise ValueError("public key data is too short")
    # Keep only the type/data. A comment is unnecessary and may contain personal data.
    return " ".join(parts[:2])


def network_config(role: str, mac: str) -> dict:
    return {
        "version": 2,
        "ethernets": {
            "warehouse": {
                "match": {"macaddress": mac},
                "dhcp4": False,
                "dhcp6": False,
                "addresses": [ROLES[role]["staging_ip"] + "/24"],
                "routes": [{"to": "0.0.0.0/0", "via": NAT_GATEWAY}],
                "nameservers": {"addresses": [NAT_GATEWAY]},
            }
        },
    }


def networkd_config(role: str, mac: str, mode: str) -> str:
    lines = [
        "# Generated only for a disposable warehouse VMware guest.",
        "[Match]", f"MACAddress={mac}", "", "[Link]", "RequiredForOnline=yes",
        "", "[Network]", "IPv6AcceptRA=no", "LinkLocalAddressing=no",
    ]
    address = ROLES[role]["staging_ip"] if mode == "staging" else ROLES[role]["final_ip"]
    if address:
        gateway = NAT_GATEWAY if mode == "staging" else ROLES[role]["gateway"]
        lines.extend(["DHCP=no", f"Address={address}/24", f"Gateway={gateway}", f"DNS={gateway}"])
    else:
        lines.extend(["DHCP=ipv4", "", "[DHCPv4]", "UseDNS=yes", "UseRoutes=yes"])
    return "\n".join(lines) + "\n"


def switch_script(role: str, mac: str) -> str:
    """Prepare the next boot, without restarting the live staging NIC."""
    return """#!/bin/sh
set -eu
if [ "$(id -u)" != 0 ]; then echo 'Run this guest-only command with sudo.' >&2; exit 1; fi
if ! grep -qi vmware /sys/class/dmi/id/sys_vendor || [ ! -f /etc/warehouse-guest.json ]; then
    echo 'Refusing to configure a non-warehouse VMware guest.' >&2; exit 1
fi
mode=${1:-}
case "$mode" in final|staging) ;; *) echo 'Usage: warehouse-switch-network final|staging [--poweroff]' >&2; exit 2 ;; esac
if [ "$#" -gt 2 ] || { [ "$#" -eq 2 ] && [ "$2" != --poweroff ]; }; then
    echo 'Only --poweroff is accepted after the mode.' >&2; exit 2
fi
if [ ! -x /usr/lib/systemd/systemd-networkd ]; then
    echo 'systemd-networkd is unavailable; no network configuration was changed.' >&2; exit 1
fi
if [ ! -f /var/lib/warehouse/bootstrap.json ] || ! python3 -c 'import json; assert json.load(open("/var/lib/warehouse/bootstrap.json"))["status"] == "ready"'; then
    echo 'Bootstrap is not ready; complete NAT provisioning first.' >&2; exit 1
fi
backup=/var/lib/warehouse/network-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$
install -d -m 0700 "$backup"
for item in /etc/network/interfaces /etc/cloud/cloud.cfg.d/99-warehouse-network.cfg /etc/systemd/network/05-warehouse-lab.network /etc/systemd/resolved.conf.d/warehouse-lab.conf; do
    if [ -e "$item" ]; then cp -a "$item" "$backup/$(echo "$item" | tr / _)"; fi
done
install -d /etc/cloud/cloud.cfg.d /etc/systemd/network /etc/systemd/resolved.conf.d
printf 'network: {config: disabled}\n' > /etc/cloud/cloud.cfg.d/99-warehouse-network.cfg
install -m 0644 "/var/lib/warehouse/network/$mode.network" /etc/systemd/network/05-warehouse-lab.network
# Leave existing live networking intact until poweroff, but do not let ifupdown
# apply the former NAT address alongside networkd at the next boot.
if [ -d /etc/network ]; then
    printf 'auto lo\niface lo inet loopback\n' > /etc/network/interfaces
fi
if systemctl cat networking.service >/dev/null 2>&1; then systemctl disable networking.service; fi
systemctl enable systemd-networkd.service systemd-networkd-wait-online.service systemd-resolved.service
if [ "$mode" = staging ]; then dns=@@STAGING_DNS@@; else dns=@@FINAL_DNS@@; fi
printf '[Resolve]\nDNS=%s\nFallbackDNS=\n' "$dns" > /etc/systemd/resolved.conf.d/warehouse-lab.conf
ln -sfn /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
printf '{"role":"@@ROLE@@","mode":"%s","state":"prepared_for_next_boot","mac":"@@MAC@@"}\n' "$mode" > /var/lib/warehouse/network-pending.json
echo "Prepared $mode network for the next boot. Live interface was not restarted."
echo 'Power off the VM, change its ONLY NIC to the selected LAN Segment/NAT, then boot.'
if [ "${2:-}" = --poweroff ]; then systemctl poweroff; fi
""".replace("@@ROLE@@", role).replace("@@MAC@@", mac).replace("@@FINAL_DNS@@", str(ROLES[role]["gateway"])).replace("@@STAGING_DNS@@", NAT_GATEWAY)


def bootstrap_script(role: str) -> str:
    packages = BASE_PACKAGES + ROLE_PACKAGES[role]
    service_checks = ""
    if role == "service":
        service_checks = """
usermod -aG docker warehouse
systemctl enable --now docker.service
docker info >/dev/null
docker compose version >/dev/null
"""
    elif role == "iot":
        service_checks = "python3 -c 'import paho.mqtt.client'\n"
    elif role == "office":
        service_checks = """
command -v firefox-esr >/dev/null
command -v startxfce4 >/dev/null
systemctl enable lightdm.service
systemctl set-default graphical.target
"""
    return """#!/bin/sh
set -eu
if [ "$(id -u)" != 0 ]; then exit 1; fi
if ! grep -qi vmware /sys/class/dmi/id/sys_vendor || [ ! -f /etc/warehouse-guest.json ]; then
    echo 'Refusing bootstrap outside the disposable warehouse VMware guest.' >&2; exit 1
fi
install -d /run/lock
exec 9>/run/lock/warehouse-bootstrap.lock
if ! flock -n 9; then
    echo 'WAREHOUSE_BOOTSTRAP_ALREADY_RUNNING' >&2; exit 75
fi
origin=manual
if [ "$#" -ne 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != --origin ]; then exit 2; fi
    origin=$2
fi
case "$origin" in cloud-init|manual) ;; *) exit 2 ;; esac
export WAREHOUSE_BOOTSTRAP_ORIGIN="$origin"
install -d -m 0755 /var/lib/warehouse
printf '{"role":"@@ROLE@@","status":"running","origin":"%s"}\n' "$origin" > /var/lib/warehouse/bootstrap.json
cp /var/lib/warehouse/bootstrap.json "/var/lib/warehouse/bootstrap-$origin.json"
finish() {
    code=$?
    trap - EXIT
    if [ "$code" -ne 0 ]; then
        printf '{"role":"@@ROLE@@","status":"failed","exit_code":%s,"origin":"%s"}\n' "$code" "$origin" > /var/lib/warehouse/bootstrap.json
        cp /var/lib/warehouse/bootstrap.json "/var/lib/warehouse/bootstrap-$origin.json"
    fi
    exit "$code"
}
trap finish EXIT
# The VMware COM1 pipe is a local console, not an extra IP interface.
systemctl daemon-reload
systemctl enable --now serial-getty@ttyS0.service
systemctl restart serial-getty@ttyS0.service
if command -v update-grub >/dev/null 2>&1; then update-grub; fi
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
timeout 240 apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o DPkg::Lock::Timeout=60 -o APT::Update::Error-Mode=any update
timeout 900 apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o DPkg::Lock::Timeout=60 install -y --no-install-recommends @@PACKAGES@@
sshd -t
systemctl enable --now ssh.service open-vm-tools.service
@@SERVICE_CHECKS@@
apt-get clean
python3 - <<'PY'
from datetime import datetime, timezone
import json, os
p='/var/lib/warehouse/bootstrap.json'
with open(p+'.tmp','w') as f:
    json.dump({'role':'@@ROLE@@','status':'ready','origin':os.environ['WAREHOUSE_BOOTSTRAP_ORIGIN'],'completed_at':datetime.now(timezone.utc).isoformat()},f)
os.replace(p+'.tmp',p)
import shutil
shutil.copyfile(p, '/var/lib/warehouse/bootstrap-'+os.environ['WAREHOUSE_BOOTSTRAP_ORIGIN']+'.json')
PY
echo 'WAREHOUSE_BOOTSTRAP_READY role=@@ROLE@@'
""".replace("@@ROLE@@", role).replace("@@PACKAGES@@", " ".join(shlex.quote(p) for p in packages)).replace("@@SERVICE_CHECKS@@", service_checks)


def cloud_config(role: str, mac: str, public_key: str) -> dict:
    files = [
        {"path": "/etc/warehouse-guest.json", "permissions": "0644", "content": json.dumps({"role": role, "mac": mac, "lab": "warehouse-netops"}) + "\n"},
        {"path": "/etc/ssh/sshd_config.d/10-warehouse-lab.conf", "permissions": "0644", "content": "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nAllowUsers warehouse\n"},
        {"path": "/etc/systemd/resolved.conf.d/warehouse-lab.conf", "permissions": "0644", "content": f"[Resolve]\nDNS={NAT_GATEWAY}\nFallbackDNS=\n"},
        {"path": "/etc/systemd/system/serial-getty@ttyS0.service.d/warehouse-autologin.conf", "permissions": "0644", "content": "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin warehouse --noclear --keep-baud 115200,38400,9600 %I $TERM\n"},
        {"path": "/etc/default/grub.d/99-warehouse-serial.cfg", "permissions": "0644", "content": 'case " ${GRUB_CMDLINE_LINUX:-} " in\n  *" console=ttyS0,"*) ;;\n  *) GRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX:-} console=ttyS0,115200n8" ;;\nesac\n'},
        {"path": "/usr/local/sbin/warehouse-bootstrap", "permissions": "0755", "content": bootstrap_script(role)},
        {"path": "/usr/local/sbin/warehouse-switch-network", "permissions": "0755", "content": switch_script(role, mac)},
        {"path": "/var/lib/warehouse/network/staging.network", "permissions": "0644", "content": networkd_config(role, mac, "staging")},
        {"path": "/var/lib/warehouse/network/final.network", "permissions": "0644", "content": networkd_config(role, mac, "final")},
    ]
    if role == "office":
        files.append({
            "path": "/etc/lightdm/lightdm.conf.d/50-warehouse-lab.conf", "permissions": "0644",
            "content": "[Seat:*]\nautologin-user=warehouse\nautologin-user-timeout=0\nuser-session=xfce\nautologin-session=xfce\n",
        })
    return {
        "hostname": "warehouse-" + role,
        "manage_etc_hosts": True,
        "timezone": "Asia/Shanghai",
        "disable_root": True,
        "ssh_pwauth": False,
        "users": [{
            "name": "warehouse", "gecos": "Disposable warehouse laboratory user",
            "shell": "/bin/bash", "groups": ["sudo"], "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
            "lock_passwd": True, "ssh_authorized_keys": [public_key],
        }],
        "write_files": files,
        "runcmd": [["/usr/local/sbin/warehouse-bootstrap", "--origin", "cloud-init"]],
        "final_message": "Warehouse cloud-init finished; verify /var/lib/warehouse/bootstrap.json and cloud-init status.",
    }


def generate_seed(role: str, output_dir: Path, public_key: str, instance_id: str, mac: str) -> dict:
    if role not in ROLES:
        raise ValueError("unknown role")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", instance_id):
        raise ValueError("instance-id must be 1-80 safe characters")
    mac = normalize_mac(mac)
    output_dir.mkdir(parents=True, exist_ok=True)
    contents = {
        "user-data": "#cloud-config\n" + json.dumps(cloud_config(role, mac, public_key), indent=2) + "\n",
        "meta-data": json.dumps({"instance-id": instance_id, "local-hostname": "warehouse-" + role}, indent=2) + "\n",
        "network-config": json.dumps(network_config(role, mac), indent=2) + "\n",
        "bootstrap.sh": bootstrap_script(role),
        "switch-network.sh": switch_script(role, mac),
        "staging.network": networkd_config(role, mac, "staging"),
        "final.network": networkd_config(role, mac, "final"),
    }
    for name, content in contents.items():
        (output_dir / name).write_text(content, encoding="utf-8", newline="\n")
    return {"role": role, "instance_id": instance_id, "mac": mac, "staging_ip": ROLES[role]["staging_ip"], "files": list(contents)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Debian 13 NoCloud seed inputs for the isolated warehouse VMs")
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ssh-public-key-file", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--mac", required=True, help="MAC of the VM's only e1000 NIC")
    args = parser.parse_args()
    try:
        result = generate_seed(args.role, args.output_dir, read_public_key(args.ssh_public_key_file), args.instance_id, args.mac)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
