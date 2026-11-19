#!/bin/sh
# OpenWrt 23.05+ / firewall4, dedicated classroom router.
set -eu
umask 077

STAGES=/root/warehouse-netops-stages
ACTIVE=/tmp/warehouse-netops-active
CONFIGS='network wireless dhcp firewall'

die() { printf '%s\n' "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }
read_uci() { uci -q get "$1" 2>/dev/null || :; }
u() { uci -c "$CONFIG" -t "$DELTA" "$@"; }
delete() { u -q delete "$1" 2>/dev/null || :; }

check_stage() {
    [ -d "$1" ] || die 'Stage directory not found'
    stage=$(readlink -f "$1")
    case "$stage" in "$STAGES"/*) ;; *) die "Stage must be beneath $STAGES" ;; esac
    [ -f "$stage/.prepared" ] || die 'Stage is incomplete'
    [ -f "$stage/lab-router.sh" ] || die 'Stage lacks rollback program'
}

check_tools() {
    for cmd in uci ubus jsonfilter ip iw wg fw4 dnsmasq base64 dd readlink cmp nohup timeout; do need "$cmd"; done
    [ -x /etc/init.d/network ] || die 'OpenWrt netifd is required'
    [ -x /etc/init.d/firewall ] || die 'OpenWrt firewall4 is required'
    [ -f /etc/openwrt_release ] || die 'Run this script on OpenWrt'
    [ "$(id -u)" = 0 ] || die 'Run as root on the dedicated lab router'
    modprobe wireguard 2>/dev/null || die 'Install the matching kmod-wireguard package first'
    [ -z "$(uci changes)" ] || die 'Save or revert pending LuCI/UCI edits before preparing'
}

prepare() {
    check_tools
    radio=${1:-}
    lan_device=$(read_uci network.lan.device)
    [ -n "$lan_device" ] || die 'network.lan.device is missing; this script requires a modern bridge LAN'
    wan_device=$(read_uci network.wan.device)
    [ -n "$wan_device" ] || die 'network.wan.device is missing; confirm the WAN device in LuCI first'
    [ "$wan_device" != "$lan_device" ] || die 'LAN and WAN cannot share the same device'
    for name in $CONFIGS; do [ -f "/etc/config/$name" ] || die "Missing /etc/config/$name"; done
    # Preserve real switch/DSA VLAN mappings and ports. Avoid silently discarding extra networks.
    for section in $(uci show network | sed -n "s/^network\.\([^.=]*\)=interface$/\1/p"); do
        case "$section" in loopback|lan|wan|wan6) ;; *) die "Extra network interface '$section' exists; use a dedicated lab configuration" ;; esac
    done
    bridges=$(uci show network | sed -n "s/^network\.\([^.=]*\)=device$/\1/p")
    lan_bridge=
    for item in $bridges; do
        if [ "$(read_uci "network.$item.name")" = "$lan_device" ]; then
            [ "$(read_uci "network.$item.type")" = bridge ] || die 'LAN device must be an existing wired bridge'
            lan_bridge=$item
        fi
    done
    [ -n "$lan_bridge" ] || die 'Could not identify existing LAN bridge; device-specific review is needed'
    radios=$(uci show wireless | sed -n "s/^wireless\.\([^.=]*\)=wifi-device$/\1/p")
    candidates=
    for item in $radios; do
        band=$(read_uci "wireless.$item.band")
        hwmode=$(read_uci "wireless.$item.hwmode")
        if [ "$band" = 2g ] || [ "$hwmode" = 11g ]; then candidates="$candidates $item"; fi
    done
    if [ -z "$radio" ]; then
        set -- $candidates
        [ "$#" = 1 ] || die 'Specify --radio with the verified 2.4 GHz radio name from uci show wireless'
        radio=$1
    else
        case " $candidates " in *" $radio "*) ;; *) die 'Selected radio is not configured for 2.4 GHz' ;; esac
    fi
    iw phy >/dev/null 2>&1 || die 'Wireless driver is unavailable; inspect iw phy before continuing'
    for section in warehouse_office warehouse_iot; do
        [ -z "$(read_uci "wireless.$section")" ] || die 'Already has warehouse wireless sections'
    done
    mkdir -p "$STAGES"
    stage="$STAGES/$(date +%Y%m%d-%H%M%S)-$$"
    mkdir -p "$stage/config" "$stage/backup" "$stage/delta" "$stage/private"
    CONFIG="$stage/config"; DELTA="$stage/delta"
    for name in $CONFIGS; do
        cp "/etc/config/$name" "$stage/backup/$name"
        cp "/etc/config/$name" "$CONFIG/$name"
    done
    cp "$0" "$stage/lab-router.sh"
    chmod 700 "$stage/lab-router.sh"
    wg genkey > "$stage/private/router.key"
    wg pubkey < "$stage/private/router.key" > "$stage/private/router.pub"
    wg genkey > "$stage/private/client.key"
    wg pubkey < "$stage/private/client.key" > "$stage/private/client.pub"
    office_key=$(dd if=/dev/urandom bs=18 count=1 2>/dev/null | base64)
    iot_key=$(dd if=/dev/urandom bs=18 count=1 2>/dev/null | base64)
    printf 'Warehouse-Office=%s\nWarehouse-IoT=%s\n' "$office_key" "$iot_key" > "$stage/private/wifi-passwords.txt"
    cat > "$stage/private/warehouse-admin.conf" <<EOF
[Interface]
PrivateKey = $(cat "$stage/private/client.key")
Address = 10.66.66.2/32

[Peer]
PublicKey = $(cat "$stage/private/router.pub")
Endpoint = 172.31.250.1:51820
AllowedIPs = 192.168.30.10/32
PersistentKeepalive = 25
EOF

    u set network.lan.proto=static
    u set network.lan.ipaddr=192.168.30.1
    u set network.lan.netmask=255.255.255.0
    delete network.lan.gateway
    delete network.lan.dns
    delete network.lan.ip6assign
    delete network.lan.ip6addr
    delete network.lan.ip6hint
    delete network.globals.ula_prefix
    u set network.lan.delegate=0
    u set network.wan.proto=static
    u set network.wan.ipaddr=172.31.250.1
    u set network.wan.netmask=255.255.255.0
    delete network.wan.gateway
    delete network.wan.dns
    delete network.wan.ip6assign
    delete network.wan.ip6addr
    u set network.wan.delegate=0
    delete network.wan6
    for spec in 'office 192.168.10.1 br-office' 'iot 192.168.20.1 br-iot'; do
        set -- $spec; name=$1; address=$2; bridge=$3
        u set "network.warehouse_${name}_bridge=device"
        u set "network.warehouse_${name}_bridge.name=$bridge"
        u set "network.warehouse_${name}_bridge.type=bridge"
        u set "network.warehouse_${name}_bridge.bridge_empty=1"
        u set "network.$name=interface"
        u set "network.$name.device=$bridge"
        u set "network.$name.proto=static"
        u set "network.$name.ipaddr=$address"
        u set "network.$name.netmask=255.255.255.0"
        u set "network.$name.delegate=0"
    done
    u set network.wg_admin=interface
    u set network.wg_admin.proto=wireguard
    u set "network.wg_admin.private_key=$(cat "$stage/private/router.key")"
    u set network.wg_admin.listen_port=51820
    u add_list network.wg_admin.addresses=10.66.66.1/24
    u set network.warehouse_admin_peer=wireguard_wg_admin
    u set "network.warehouse_admin_peer.public_key=$(cat "$stage/private/client.pub")"
    u add_list network.warehouse_admin_peer.allowed_ips=10.66.66.2/32
    u set network.warehouse_admin_peer.route_allowed_ips=1

    # Replace BSS sections while keeping device-specific radio paths, channels and country settings.
    while :; do
        item=$(u show wireless | sed -n "s/^wireless\.\([^.=]*\)=wifi-iface$/\1/p" | head -n 1)
        [ -n "$item" ] || break
        u delete "wireless.$item"
    done
    for item in $radios; do u set "wireless.$item.disabled=1"; done
    u set "wireless.$radio.disabled=0"
    for spec in 'office Warehouse-Office' 'iot Warehouse-IoT'; do
        set -- $spec; name=$1; ssid=$2
        u set "wireless.warehouse_$name=wifi-iface"
        u set "wireless.warehouse_$name.device=$radio"
        u set "wireless.warehouse_$name.mode=ap"
        u set "wireless.warehouse_$name.network=$name"
        u set "wireless.warehouse_$name.ssid=$ssid"
        u set "wireless.warehouse_$name.encryption=psk2"
        u set "wireless.warehouse_$name.isolate=1"
        case "$name" in office) key=$office_key ;; iot) key=$iot_key ;; esac
        u set "wireless.warehouse_$name.key=$key"
    done
    cat > "$CONFIG/dhcp" <<'EOF'
config dnsmasq
    option domainneeded '1'
    option boguspriv '1'
    option localservice '1'
    option domain 'warehouse.lab'
    option local '/warehouse.lab/'
    option expandhosts '1'
    option authoritative '1'
    option leasefile '/tmp/dhcp.leases'
    option resolvfile '/tmp/resolv.conf.d/resolv.conf.auto'
    list address '/warehouse.lab/192.168.30.10'

config dhcp 'lan'
    option interface 'lan'
    option ignore '1'
    option ra 'disabled'
    option dhcpv6 'disabled'
    option ndp 'disabled'

config dhcp 'office'
    option interface 'office'
    option start '100'
    option limit '50'
    option leasetime '4h'
    option ra 'disabled'
    option dhcpv6 'disabled'
    option ndp 'disabled'

config dhcp 'iot'
    option interface 'iot'
    option start '100'
    option limit '50'
    option leasetime '4h'
    option ra 'disabled'
    option dhcpv6 'disabled'
    option ndp 'disabled'
EOF
    cat > "$CONFIG/firewall" <<'EOF'
config defaults
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'
    option synflood_protect '1'
    option drop_invalid '1'

config zone
    option name 'service'
    list network 'lan'
    option input 'ACCEPT'
    option output 'ACCEPT'
    option forward 'DROP'

config zone
    option name 'office'
    list network 'office'
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'

config zone
    option name 'iot'
    list network 'iot'
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'

config zone
    option name 'wan'
    list network 'wan'
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'

config zone
    option name 'vpn'
    list network 'wg_admin'
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'

config rule
    option name 'Allow-WireGuard-from-classroom-WAN'
    option src 'wan'
    option proto 'udp'
    option dest_port '51820'
    option family 'ipv4'
    option target 'ACCEPT'

config rule
    option name 'Office-dashboard'
    option src 'office'
    option dest 'service'
    option dest_ip '192.168.30.10'
    option proto 'tcp'
    option dest_port '8080'
    option family 'ipv4'
    option target 'ACCEPT'

config rule
    option name 'IoT-MQTT'
    option src 'iot'
    option dest 'service'
    option dest_ip '192.168.30.10'
    option proto 'tcp'
    option dest_port '1883'
    option family 'ipv4'
    option target 'ACCEPT'

config rule
    option name 'VPN-dashboard-SSH'
    option src 'vpn'
    option dest 'service'
    option dest_ip '192.168.30.10'
    option proto 'tcp'
    option dest_port '22 8080'
    option family 'ipv4'
    option target 'ACCEPT'
EOF
    for name in office iot; do
        cat >> "$CONFIG/firewall" <<EOF

config rule
    option name '$name-DHCP'
    option src '$name'
    option proto 'udp'
    option dest_port '67'
    option family 'ipv4'
    option target 'ACCEPT'

config rule
    option name '$name-DNS'
    option src '$name'
    option proto 'tcp udp'
    option dest_port '53'
    option family 'ipv4'
    option target 'ACCEPT'
EOF
    done
    for name in $CONFIGS; do u commit "$name"; done
    printf '%s\n' "radio=$radio" "service_device=$lan_device" "wan_device=$wan_device" > "$stage/summary.txt"
    touch "$stage/.prepared"
    printf 'Prepared %s\n' "$stage"
    printf 'Inspect staged files and private/ locally. Apply with: sh %s apply %s\n' "$0" "$stage"
    printf 'Service PC must be statically 192.168.30.10/24, gateway 192.168.30.1.\n'
}

apply() {
    check_stage "$1"
    need fw4
    [ ! -d "$ACTIVE" ] || die 'Another lab apply is awaiting confirmation or rollback'
    for name in $CONFIGS; do
        cmp -s "/etc/config/$name" "$stage/backup/$name" || die "Live $name changed since prepare; create a fresh stage"
    done
    [ -z "${SSH_CONNECTION:-}" ] && die 'Apply from wired SSH on the existing service bridge'
    client_ip=${SSH_CONNECTION%% *}
    lan_device=$(uci -q get network.lan.device)
    route=$(ip -4 route get "$client_ip")
    case " $route " in *" dev $lan_device "*) ;; *) die 'SSH must arrive through the existing LAN bridge' ;; esac
    case " $route " in *' via '*) die 'Use a directly attached wired management host' ;; esac
    mkdir "$ACTIVE"
    printf '%s\n' "$stage" > "$ACTIVE/stage"
    # nohup survives the SSH disconnect caused by changing the service address.
    nohup sh "$stage/lab-router.sh" timeout "$stage" > "$stage/rollback.log" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$ACTIVE/watchdog.pid"
    nohup sh "$stage/lab-router.sh" activate "$stage" > "$stage/apply.log" 2>&1 < /dev/null &
    printf 'Application started. Reconnect from wired 192.168.30.10 to 192.168.30.1 and run confirm within 180 seconds.\n'
    printf 'Application log: %s/apply.log\n' "$stage"
}

activate() {
    check_stage "$1"
    [ -f "$ACTIVE/stage" ] && [ "$(cat "$ACTIVE/stage")" = "$stage" ] || die 'No matching apply transaction'
    # The worker survives SSH loss. Any command failure restores the saved configuration.
    trap 'result=$?; if [ "$result" -ne 0 ]; then sh "$stage/lab-router.sh" rollback "$stage"; fi' 0
    for name in $CONFIGS; do cp "$stage/config/$name" "/etc/config/$name"; done
    # Syntax validation before any interface/firewall reload.
    if ! timeout 15 fw4 check > "$stage/firewall-check.log" 2>&1; then
        die 'firewall4 validation failed; restoring original configuration'
    fi
    timeout 25 /etc/init.d/network reload
    timeout 25 /etc/init.d/dnsmasq restart
    if [ -x /etc/init.d/odhcpd ]; then timeout 25 /etc/init.d/odhcpd restart; fi
    timeout 25 /etc/init.d/firewall restart
    touch "$stage/.activated"
}

confirm() {
    [ -f "$ACTIVE/stage" ] || die 'No active change to confirm'
    case "${SSH_CONNECTION:-}" in '192.168.30.'*) ;; *) die 'Confirm from a wired service-network SSH client' ;; esac
    client_ip=${SSH_CONNECTION%% *}
    route=$(ip -4 route get "$client_ip")
    lan_device=$(uci -q get network.lan.device)
    case " $route " in *" dev $lan_device "*) ;; *) die 'Confirm must arrive through the service bridge' ;; esac
    case " $route " in *' via '*) die 'Use a directly attached service host' ;; esac
    stage=$(cat "$ACTIVE/stage")
    [ -f "$stage/.activated" ] || die 'Application still running or failed; inspect apply.log'
    mkdir "$ACTIVE/decision" 2>/dev/null || die 'Timeout rollback has already begun'
    touch "$stage/.confirmed"
    rm -f "$ACTIVE/stage" "$ACTIVE/watchdog.pid"
    rmdir "$ACTIVE/decision" "$ACTIVE"
    printf 'Confirmed. Saved original configuration at %s/backup\n' "$stage"
}

rollback() {
    check_stage "$1"
    failed=0
    for name in $CONFIGS; do cp "$stage/backup/$name" "/etc/config/$name" || failed=1; done
    # Attempt every service even if one fails, and leave the transaction for recovery on error.
    timeout 25 /etc/init.d/network reload || failed=1
    timeout 25 /etc/init.d/dnsmasq restart || failed=1
    if [ -x /etc/init.d/odhcpd ]; then timeout 25 /etc/init.d/odhcpd restart || failed=1; fi
    timeout 25 /etc/init.d/firewall restart || failed=1
    if [ "$failed" != 0 ]; then
        touch "$stage/.rollback-failed"
        die "Recovery attempted but a service failed; inspect $stage and recover over wired LAN/failsafe"
    fi
    if [ -f "$ACTIVE/stage" ] && [ "$(cat "$ACTIVE/stage")" = "$stage" ]; then
        rm -f "$ACTIVE/stage" "$ACTIVE/watchdog.pid"
        rmdir "$ACTIVE/decision" 2>/dev/null || :
        rmdir "$ACTIVE" 2>/dev/null || :
    fi
    touch "$stage/.rolled-back"
    printf 'Original configuration restored from %s/backup\n' "$stage"
}

timeout_guard() {
    check_stage "$1"
    sleep 180
    if [ -f "$stage/.confirmed" ] || [ -f "$stage/.rolled-back" ]; then
        if [ -f "$ACTIVE/stage" ] && [ "$(cat "$ACTIVE/stage")" = "$stage" ]; then
            rm -f "$ACTIVE/stage" "$ACTIVE/watchdog.pid"
            rmdir "$ACTIVE" 2>/dev/null || :
        fi
    elif [ -f "$ACTIVE/stage" ] && [ "$(cat "$ACTIVE/stage")" = "$stage" ] && mkdir "$ACTIVE/decision" 2>/dev/null; then
        sh "$stage/lab-router.sh" rollback "$stage"
    fi
}

status() {
    printf '\nInterface summary\n'
    for interface in lan office iot wan wg_admin; do
        ubus call "network.interface.$interface" status 2>/dev/null | jsonfilter -e '@.interface' -e '@.up' -e '@.ipv4_address[*].address' || :
    done
    printf '\nWireGuard public state\n'
    wg show wg_admin 2>/dev/null || :
    printf '\nWireless status\n'
    wifi status | jsonfilter -e '@.*.up' -e '@.*.interfaces[*].config.ssid'
    printf '\nRecent DHCP leases\n'
    cat /tmp/dhcp.leases 2>/dev/null || :
}

case "${1:-}" in
    prepare)
        shift
        case "$#" in 0) prepare ;; 2) [ "$1" = --radio ] || die 'Expected --radio radioN'; prepare "$2" ;; *) die 'Usage: prepare [--radio radioN]' ;; esac ;;
    apply) [ "$#" = 2 ] || die 'Usage: apply STAGE_DIRECTORY'; apply "$2" ;;
    activate) [ "$#" = 2 ] || die 'Internal usage: activate STAGE_DIRECTORY'; activate "$2" ;;
    confirm) confirm ;;
    rollback) [ "$#" = 2 ] || die 'Usage: rollback STAGE_DIRECTORY'; rollback "$2" ;;
    timeout) [ "$#" = 2 ] || die 'Usage: timeout STAGE_DIRECTORY'; timeout_guard "$2" ;;
    status) status ;;
    *) printf 'Usage: sh lab-router.sh prepare [--radio radioN] | apply STAGE_DIR | confirm | rollback STAGE_DIR | status\n'; exit 2 ;;
esac
