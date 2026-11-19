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


FIREWALL_SCRIPT = r'''#!/bin/sh
# @OWNER@
set -eu
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
if [ "${WAREHOUSE_DISPOSABLE_CONTAINER:-}" = 1 ] && [ -f /.dockerenv ]; then
    PATH="${WAREHOUSE_TEST_BIN:?}:$PATH"
fi
export PATH
OWNER='@OWNER@'
STATE=/var/lib/warehouse-netops-guangzhou
IFACE=wg-warehouse
CHAIN=WH-WAREHOUSE-IN
RAW_CHAIN=WH-WAREHOUSE-RAW
WG_UNIT=wg-quick@wg-warehouse.service
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die 'Run as root'
[ ! -L "$STATE" ] && [ -f "$STATE/.owner" ] && [ "$(cat "$STATE/.owner")" = "$OWNER" ] || die 'Missing matching project ownership marker'
ACTION=${1:-}
case "$ACTION" in
    up|reconcile)
        trap 'result=$?; trap - 0; if [ "$result" -ne 0 ] && systemctl is-active --quiet "$WG_UNIT"; then systemctl stop "$WG_UNIT" || :; fi; exit "$result"' 0 ;;
esac
exec 9>/run/warehouse-wg-firewall.lock
flock -w 15 9 || die 'Project firewall operation is already running'

exists() { iptables -w 10 -t "$1" -S "$2" >/dev/null 2>&1; }
owned_chain() {
    table=$1; chain=$2
    exists "$table" "$chain" || return 0
    iptables -w 10 -t "$table" -S "$chain" | python3 -c '
import shlex,sys
chain,owner=sys.argv[1:]
seen=False
for line in sys.stdin:
    rule=shlex.split(line)
    seen=True
    if rule==["-N",chain]: continue
    if rule[:2]==["-A",chain] and "--comment" in rule and rule[rule.index("--comment")+1]==owner: continue
    raise SystemExit("Unknown rule in project-named firewall chain")
if not seen: raise SystemExit("Could not inspect project firewall chain")
' "$chain" "$OWNER"
}
remove_hooks() {
    table=$1; parent=$2; chain=$3
    while iptables -w 10 -t "$table" -C "$parent" -m comment --comment "$OWNER" -j "$chain" 2>/dev/null; do
        iptables -w 10 -t "$table" -D "$parent" -m comment --comment "$OWNER" -j "$chain" || return 1
    done
}
first_hook() {
    table=$1; parent=$2; chain=$3
    iptables -w 10 -t "$table" -S "$parent" | python3 -c '
import shlex,sys
parent,chain,owner=sys.argv[1:]
rules=[shlex.split(line) for line in sys.stdin if line.startswith("-A ")]
expected=["-A",parent,"-m","comment","--comment",owner,"-j",chain]
raise SystemExit(0 if rules and rules[0]==expected and rules.count(expected)==1 else 1)
' "$parent" "$chain" "$OWNER"
}
ensure_hook() {
    table=$1; parent=$2; chain=$3
    if ! first_hook "$table" "$parent" "$chain"; then
        remove_hooks "$table" "$parent" "$chain" || return 1
        iptables -w 10 -t "$table" -I "$parent" 1 -m comment --comment "$OWNER" -j "$chain"
    fi
}
filter_correct() {
    exists filter "$CHAIN" || return 1
    [ "$(iptables -w 10 -t filter -S "$CHAIN" | grep -c '^-A ' || :)" = 4 ] || return 1
    iptables -w 10 -t filter -C "$CHAIN" -i "$IFACE" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$OWNER" -j ACCEPT &&
    iptables -w 10 -t filter -C "$CHAIN" -i "$IFACE" -m comment --comment "$OWNER" -j DROP &&
    iptables -w 10 -t filter -C "$CHAIN" -i '@WAN_INTERFACE@' -p udp --dport 51830 -m comment --comment "$OWNER" -j ACCEPT &&
    iptables -w 10 -t filter -C "$CHAIN" -m comment --comment "$OWNER" -j RETURN
}
raw_correct() {
    exists raw "$RAW_CHAIN" || return 1
    [ "$(iptables -w 10 -t raw -S "$RAW_CHAIN" | grep -c '^-A ' || :)" = 2 ] || return 1
    iptables -w 10 -t raw -C "$RAW_CHAIN" -i "$IFACE" ! -d 10.89.0.1/32 -m comment --comment "$OWNER" -j DROP &&
    iptables -w 10 -t raw -C "$RAW_CHAIN" -m comment --comment "$OWNER" -j RETURN
}
check() {
    owned_chain filter "$CHAIN" || return 1
    owned_chain raw "$RAW_CHAIN" || return 1
    filter_correct && raw_correct && first_hook filter INPUT "$CHAIN" && first_hook raw PREROUTING "$RAW_CHAIN"
}
up() {
    owned_chain filter "$CHAIN" || return 1
    owned_chain raw "$RAW_CHAIN" || return 1
    # Raw INPUT-destination guard prevents transit through existing Docker rules.
    # No FORWARD/NAT/sysctl/default-route change is made.
    if ! raw_correct; then
        if exists raw "$RAW_CHAIN"; then
            remove_hooks raw PREROUTING "$RAW_CHAIN"
            iptables -w 10 -t raw -F "$RAW_CHAIN"
        else iptables -w 10 -t raw -N "$RAW_CHAIN"; fi
        iptables -w 10 -t raw -A "$RAW_CHAIN" -i "$IFACE" ! -d 10.89.0.1/32 -m comment --comment "$OWNER" -j DROP
        iptables -w 10 -t raw -A "$RAW_CHAIN" -m comment --comment "$OWNER" -j RETURN
    fi
    ensure_hook raw PREROUTING "$RAW_CHAIN"
    if ! filter_correct; then
        if exists filter "$CHAIN"; then
            remove_hooks filter INPUT "$CHAIN"
            iptables -w 10 -t filter -F "$CHAIN"
        else iptables -w 10 -t filter -N "$CHAIN"; fi
        iptables -w 10 -t filter -A "$CHAIN" -i "$IFACE" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$OWNER" -j ACCEPT
        iptables -w 10 -t filter -A "$CHAIN" -i "$IFACE" -m comment --comment "$OWNER" -j DROP
        iptables -w 10 -t filter -A "$CHAIN" -i '@WAN_INTERFACE@' -p udp --dport 51830 -m comment --comment "$OWNER" -j ACCEPT
        iptables -w 10 -t filter -A "$CHAIN" -m comment --comment "$OWNER" -j RETURN
    fi
    ensure_hook filter INPUT "$CHAIN"
    check
}
down() {
    owned_chain filter "$CHAIN"
    owned_chain raw "$RAW_CHAIN"
    remove_hooks filter INPUT "$CHAIN"
    remove_hooks raw PREROUTING "$RAW_CHAIN"
    for spec in "filter $CHAIN" "raw $RAW_CHAIN"; do
        set -- $spec
        if exists "$1" "$2"; then
            iptables -w 10 -t "$1" -F "$2"
            iptables -w 10 -t "$1" -X "$2"
        fi
    done
}
case "${1:-}" in
    up) up ;;
    down) down ;;
    check) check ;;
    reconcile) if systemctl is-active --quiet "$WG_UNIT"; then up; fi ;;
    *) die 'Expected up, down, check or reconcile' ;;
esac
'''


INSTALL_SCRIPT = r'''#!/bin/sh
# @OWNER@
set -eu
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
if [ "${WAREHOUSE_DISPOSABLE_CONTAINER:-}" = 1 ] && [ -f /.dockerenv ]; then
    PATH="${WAREHOUSE_TEST_BIN:?}:$PATH"
fi
export PATH
OWNER='@OWNER@'
STATE=/var/lib/warehouse-netops-guangzhou
PRIVATE=$STATE/private
LIB=/usr/local/lib/warehouse-netops-guangzhou
IFACE=wg-warehouse
FW=warehouse-wg-firewall.service
WG=wg-quick@wg-warehouse.service
TIMER=warehouse-wg-reconcile.timer
RECONCILE=warehouse-wg-reconcile.service
DROPIN=/etc/systemd/system/wg-quick@wg-warehouse.service.d
LOCK=/run/warehouse-netops-guangzhou.lock
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
owns_file() { [ -f "$1" ] && [ ! -L "$1" ] && head -n 2 "$1" | grep -Fqx "# $OWNER"; }
owned_state() { [ -d "$STATE" ] && [ ! -L "$STATE" ] && [ -f "$STATE/.owner" ] && [ "$(cat "$STATE/.owner")" = "$OWNER" ]; }
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
[ "$#" = 3 ] && [ "$2" = --project-dir ] || die 'Usage: install.sh check|install|status|rollback --project-dir ABSOLUTE_BUNDLE_DIRECTORY'
ACTION=$1
[ "$(readlink -f "$3")" = "$SCRIPT_DIR" ] || die 'Explicit project directory must be this bundle directory'
[ -f "$SCRIPT_DIR/.project-marker" ] && [ "$(cat "$SCRIPT_DIR/.project-marker")" = "$OWNER" ] || die 'Missing explicit bundle marker'
[ "$(id -u)" = 0 ] || die 'Run as root'
bundle_mode=$(stat -c '%a' "$SCRIPT_DIR")
key_mode=$(stat -c '%a' "$SCRIPT_DIR/wg-warehouse.conf")
[ "$((0$bundle_mode & 077))" = 0 ] && [ "$((0$key_mode & 077))" = 0 ] || die 'Uploaded bundle directory and private configuration must deny group/other access'
. /etc/os-release
[ "$ID" = debian ] && [ "$VERSION_ID" = 13 ] || die 'This bundle requires Debian 13'
for cmd in iptables iptables-save ip ss wg wg-quick systemctl python3 sha256sum install flock; do
    command -v "$cmd" >/dev/null 2>&1 || die "Missing prerequisite: $cmd"
done
(cd "$SCRIPT_DIR" && sha256sum -c --status checksums.txt) || die 'Bundle checksum verification failed'

resource_files() {
    printf '%s\n' "$PRIVATE/wg-warehouse.conf" "$LIB/firewall.sh" \
        "/etc/systemd/system/$FW" "/etc/systemd/system/$TIMER" \
        "/etc/systemd/system/$RECONCILE" "$DROPIN/warehouse.conf"
}
preflight() {
    OWNED=0; owned_state && OWNED=1
    iptables -w 10 -t filter -S INPUT >/dev/null || die 'Cannot inspect the existing INPUT rules'
    iptables -w 10 -t raw -S PREROUTING >/dev/null || die 'Cannot inspect the existing raw table'
    if [ -e "$STATE" ] && [ "$OWNED" = 0 ]; then die 'Unknown project state directory already exists'; fi
    for path in "$PRIVATE" "$LIB" "$DROPIN"; do [ ! -L "$path" ] || die 'A project directory is a symlink'; done
    if [ -d "$LIB" ] && { [ ! -f "$LIB/.owner" ] || [ "$(cat "$LIB/.owner")" != "$OWNER" ]; }; then die 'Unknown runtime directory already exists'; fi
    resource_files | while IFS= read -r path; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            [ "$OWNED" = 1 ] && owns_file "$path" || die "Unknown resource already exists: $path"
            cmp -s "$path" "$SCRIPT_DIR/${path##*/}" || die 'Existing project files differ from this bundle; retain the original bundle and roll back before replacement'
        fi
    done
    if [ -d "$DROPIN" ]; then
        for path in "$DROPIN"/*; do
            [ -e "$path" ] || continue
            [ "$path" = "$DROPIN/warehouse.conf" ] && owns_file "$path" || die 'Unknown WireGuard instance drop-in exists'
        done
    fi
    for unit in "$FW" "$TIMER" "$RECONCILE"; do
        fragment=$(systemctl show -p FragmentPath --value "$unit" 2>/dev/null || :)
        if [ -n "$fragment" ]; then
            [ "$OWNED" = 1 ] && [ "$fragment" = "/etc/systemd/system/$unit" ] && owns_file "$fragment" || die 'An unknown loaded unit uses a project service name'
        fi
    done
    fragment=$(systemctl show -p FragmentPath --value "$WG" 2>/dev/null || :)
    case "$fragment" in ''|/lib/systemd/system/wg-quick@.service|/usr/lib/systemd/system/wg-quick@.service) ;; *) die 'An unknown dedicated unit overrides the project WireGuard instance' ;; esac
    if [ "$OWNED" = 0 ] && { systemctl is-active --quiet "$WG" || systemctl is-enabled --quiet "$WG"; }; then
        die 'An unowned WireGuard instance is active or enabled'
    fi
    [ ! -e /etc/wireguard/wg-warehouse.conf ] && [ ! -L /etc/wireguard/wg-warehouse.conf ] || die 'An unknown standard WireGuard configuration uses this interface name'
    ip link show dev '@WAN_INTERFACE@' >/dev/null 2>&1 || die 'Expected WAN interface is missing'
    if ip link show dev "$IFACE" >/dev/null 2>&1; then
        [ "$OWNED" = 1 ] && owns_file "$PRIVATE/wg-warehouse.conf" || die 'Unknown interface already exists'
        [ "$(wg show "$IFACE" listen-port)" = 51830 ] || die 'Existing project interface has an unexpected port'
    fi
    for spec in 'filter WH-WAREHOUSE-IN' 'raw WH-WAREHOUSE-RAW'; do
        set -- $spec
        if iptables -w 10 -t "$1" -S "$2" >/dev/null 2>&1; then
            [ "$OWNED" = 1 ] || die 'Unknown project-named firewall chain exists'
            iptables -w 10 -t "$1" -S "$2" | python3 -c '
import shlex,sys
chain,owner=sys.argv[1:]
for line in sys.stdin:
    rule=shlex.split(line)
    if rule==["-N",chain]: continue
    if rule[:2]==["-A",chain] and "--comment" in rule and rule[rule.index("--comment")+1]==owner: continue
    raise SystemExit("Unknown rule in project-named firewall chain")
' "$2" "$OWNER"
        fi
    done
    listeners=$(ss -H -lun) || die 'Cannot inspect UDP listeners'
    if printf '%s\n' "$listeners" | grep -Eq '(^|[[:space:]])[^[:space:]]*:51830([[:space:]]|$)'; then
        [ "$OWNED" = 1 ] && [ "$(wg show "$IFACE" listen-port 2>/dev/null)" = 51830 ] || die 'UDP 51830 is already occupied'
    fi
    python3 - "$OWNED" <<'PY'
import ipaddress,json,subprocess,sys
owned=sys.argv[1]=='1'
reserved=ipaddress.ip_network('10.89.0.0/24')
service=ipaddress.ip_network('192.168.30.10/32')
expected={'10.89.0.1/32','10.89.0.2/32','192.168.30.10/32'}
routes=json.loads(subprocess.check_output(['ip','-j','-4','route','show','table','all']))
for route in routes:
    dst=route.get('dst','default')
    if dst=='default': continue
    network=ipaddress.ip_network(dst,strict=False)
    if not (network.overlaps(reserved) or network.overlaps(service)): continue
    if owned and route.get('dev')=='wg-warehouse' and str(network) in expected: continue
    raise SystemExit('Conflicting existing route overlaps a project network')
addresses=json.loads(subprocess.check_output(['ip','-j','-4','addr','show']))
for interface in addresses:
    for addr in interface.get('addr_info',[]):
        network=ipaddress.ip_network(str(addr['local'])+'/'+str(addr['prefixlen']),strict=False)
        if not (network.overlaps(reserved) or network.overlaps(service)): continue
        if owned and interface.get('ifname')=='wg-warehouse' and addr['local']=='10.89.0.1' and addr['prefixlen']==32: continue
        raise SystemExit('Conflicting existing address overlaps a project network')
PY
    systemctl is-active --quiet qq-stack-firewall.service || die 'Existing QQ firewall service must already be active; do not start or reconfigure it here'
    printf 'Project preflight passed. No server changes made by check.\n'
}

remove_owned_file() {
    if [ -e "$1" ] || [ -L "$1" ]; then
        owns_file "$1" || { printf 'ERROR: Refusing to remove unowned file %s\n' "$1" >&2; return 1; }
        rm -f -- "$1"
    fi
}
cleanup() {
    owned_state || { printf 'ERROR: No matching installation ownership marker\n' >&2; return 1; }
    failed=0
    for unit in "$TIMER" "$RECONCILE" "$WG"; do
        if [ -f "/etc/systemd/system/$unit" ] || { [ "$unit" = "$WG" ] && [ -f "$DROPIN/warehouse.conf" ]; }; then
            systemctl stop "$unit" || failed=1
        fi
    done
    if ip link show dev "$IFACE" >/dev/null 2>&1; then
        printf 'ERROR: Project interface remains up; retaining its firewall and private files for recovery.\n' >&2
        return 1
    fi
    if [ -f "/etc/systemd/system/$FW" ]; then systemctl stop "$FW" || failed=1; fi
    sh "$SCRIPT_DIR/firewall.sh" down || failed=1
    [ "$failed" = 0 ] || return 1
    for unit in "$TIMER" "$WG" "$FW"; do systemctl disable "$unit" >/dev/null 2>&1 || failed=1; done
    resource_files | while IFS= read -r path; do remove_owned_file "$path" || exit 1; done || failed=1
    rm -f "$STATE/.installed"
    if [ -f "$LIB/.owner" ] && [ "$(cat "$LIB/.owner")" = "$OWNER" ]; then rm -f "$LIB/.owner"; fi
    rmdir "$DROPIN" "$PRIVATE" "$LIB" 2>/dev/null || :
    systemctl daemon-reload || failed=1
    [ "$failed" = 0 ] || return 1
    touch "$STATE/.rolled-back"
    printf 'Removed only project-owned resources. Private transaction backups remain in the project state directory.\n'
}

install_bundle() {
    preflight
    mkdir "$LOCK" 2>/dev/null || die 'Another project transaction is running'
    trap 'result=$?; trap - 0 HUP INT TERM; if [ "$result" -ne 0 ]; then cleanup || :; [ ! -d "$STATE" ] || touch "$STATE/.failed"; fi; rmdir "$LOCK" 2>/dev/null || :; exit "$result"' 0
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    mkdir -p "$PRIVATE" "$LIB"
    chmod 700 "$STATE" "$PRIVATE" "$LIB"
    printf '%s\n' "$OWNER" > "$STATE/.owner"
    printf '%s\n' "$OWNER" > "$LIB/.owner"
    backup="$STATE/backup-$(date +%Y%m%d-%H%M%S)-$$"
    mkdir "$backup"
    iptables-save > "$backup/iptables.before"
    ip -j -4 route show table all > "$backup/routes.before.json"
    ip -j -4 addr show > "$backup/addresses.before.json"
    for path in /etc/wireguard/wg0.conf /etc/ssh/sshd_config; do
        if [ -f "$path" ]; then sha256sum "$path"; fi
    done > "$backup/untouched-files.sha256"
    systemctl is-active qq-stack-firewall.service > "$backup/qq-firewall.before"
    install -m 600 "$SCRIPT_DIR/wg-warehouse.conf" "$PRIVATE/wg-warehouse.conf"
    install -m 700 "$SCRIPT_DIR/firewall.sh" "$LIB/firewall.sh"
    for unit in "$FW" "$TIMER" "$RECONCILE"; do install -m 644 "$SCRIPT_DIR/$unit" "/etc/systemd/system/$unit"; done
    mkdir -p "$DROPIN"
    install -m 644 "$SCRIPT_DIR/warehouse.conf" "$DROPIN/warehouse.conf"
    systemctl daemon-reload
    systemctl enable "$FW" "$WG" "$TIMER" >/dev/null
    systemctl start "$FW"
    systemctl start "$WG"
    systemctl is-active --quiet "$FW" "$WG"
    sh "$LIB/firewall.sh" check
    [ "$(wg show "$IFACE" listen-port)" = 51830 ] || die 'Project WireGuard listener did not start'
    if [ -s "$backup/untouched-files.sha256" ]; then
        sha256sum -c --status "$backup/untouched-files.sha256" || die 'Existing protected files changed during installation'
    fi
    touch "$STATE/.installed"
    systemctl start "$TIMER"
    systemctl is-active --quiet "$FW" "$WG" "$TIMER"
    rm -f "$STATE/.rolled-back" "$STATE/.failed"
    printf 'Installed project WireGuard endpoint on UDP 51830. Cloud security-group reachability must be verified separately.\n'
}

case "$ACTION" in
    check) preflight ;;
    install) install_bundle ;;
    rollback)
        owned_state || die 'No matching installation ownership marker'
        mkdir "$LOCK" 2>/dev/null || die 'Another project transaction is running'
        trap 'result=$?; trap - 0; rmdir "$LOCK" 2>/dev/null || :; exit "$result"' 0
        cleanup ;;
    status)
        owned_state || die 'No matching installation ownership marker'
        systemctl is-active "$FW" "$WG" "$TIMER"
        sh "$SCRIPT_DIR/firewall.sh" check
        wg show "$IFACE" listen-port
        wg show "$IFACE" latest-handshakes
        ip -4 route show dev "$IFACE" ;;
    *) die 'Expected check, install, status or rollback' ;;
esac
'''


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
