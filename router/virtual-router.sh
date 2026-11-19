#!/bin/sh
# Dedicated OpenWrt x86 VM; all four Ethernet ports are selected by MAC.
# This script never configures Wi-Fi or the Windows/VMware host.
set -eu
umask 077

STAGES=/root/warehouse-virtual-stages
ACTIVE=/tmp/warehouse-virtual-active
LOCK=/tmp/warehouse-virtual-lock
CONFIGS='network dhcp firewall'
SYSNET=/sys/class/net
if [ "${WAREHOUSE_DISPOSABLE_CONTAINER:-}" = 1 ]; then
    SYSNET=${WAREHOUSE_TEST_SYSNET:-$SYSNET}
fi

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }

check_system() {
    [ "$(id -u)" = 0 ] || die 'Run as root in the dedicated OpenWrt VM'
    [ -f /etc/openwrt_release ] || die 'OpenWrt is required'
    for cmd in uci ubus jsonfilter fw4 ip readlink cmp timeout; do need "$cmd"; done
    [ -x /etc/init.d/network ] && [ -x /etc/init.d/firewall ] && [ -x /etc/init.d/dnsmasq ] || die 'netifd, firewall4 and dnsmasq are required'
    target=$(ubus call system board | jsonfilter -e '@.release.target')
    case "$target" in x86/*) ;; *) die "This script only supports OpenWrt x86 VMs (reported: $target)" ;; esac
    [ -z "$(uci changes)" ] || die 'Commit or revert pending UCI edits first'
    for name in $CONFIGS; do [ -f "/etc/config/$name" ] || die "Missing /etc/config/$name"; done
}

mac() {
    value=$(printf '%s' "$1" | tr 'A-F' 'a-f')
    printf '%s\n' "$value" | grep -Eq '^([0-9a-f]{2}:){5}[0-9a-f]{2}$' || die 'Expected a full six-octet MAC address'
    first=${value%%:*}
    [ "$((0x$first & 1))" -eq 0 ] && [ "$value" != 00:00:00:00:00:00 ] || die 'MAC addresses must be nonzero unicast addresses'
    printf '%s\n' "$value"
}

find_device() {
    wanted=$1
    found=
    for entry in "$SYSNET"/*; do
        [ -r "$entry/address" ] && [ -e "$entry/device" ] || continue
        actual=$(tr 'A-F' 'a-f' < "$entry/address")
        [ "$actual" = "$wanted" ] || continue
        [ -z "$found" ] || die "More than one hardware interface matches MAC $wanted"
        found=${entry##*/}
    done
    [ -n "$found" ] || die "No hardware interface matches MAC $wanted"
    # Only safe Linux interface characters may enter generated UCI.
    case "$found" in *[!a-zA-Z0-9_.:-]*|'') die 'Unsafe interface name' ;; esac
    printf '%s\n' "$found"
}

ipv4() {
    case "$1" in *[!0-9.]*|'') die 'WireGuard endpoint must be an IPv4 address' ;; esac
    old_ifs=$IFS; IFS=.; set -- $1; IFS=$old_ifs
    [ "$#" = 4 ] || die 'WireGuard endpoint must contain four octets'
    for octet in "$@"; do
        [ -n "$octet" ] && [ "${#octet}" -le 3 ] && [ "$octet" -le 255 ] || die 'Invalid endpoint IPv4 octet'
    done
}

check_stage() {
    [ -d "$1" ] || die 'Stage directory does not exist'
    stage=$(readlink -f "$1")
    case "$stage" in "$STAGES"/*) ;; *) die "Stage must be beneath $STAGES" ;; esac
    [ -f "$stage/.prepared" ] && [ -f "$stage/virtual-router.sh" ] || die 'Stage is incomplete'
    for name in $CONFIGS; do
        [ -f "$stage/config/$name" ] && [ -f "$stage/backup/$name" ] || die "Stage lacks $name"
    done
}

prepare() {
    wan_mac=; office_mac=; iot_mac=; service_mac=; office_client=; iot_client=
    wg_endpoint=; wg_peer_file=; wg_private_file=
    while [ "$#" -gt 0 ]; do
        [ "$#" -ge 2 ] || die 'Every prepare option requires a value'
        case "$1" in
            --wan-mac) wan_mac=$(mac "$2") ;;
            --office-mac) office_mac=$(mac "$2") ;;
            --iot-mac) iot_mac=$(mac "$2") ;;
            --service-mac) service_mac=$(mac "$2") ;;
            --office-client-mac) office_client=$(mac "$2") ;;
            --iot-client-mac) iot_client=$(mac "$2") ;;
            --wg-endpoint) wg_endpoint=$2 ;;
            --wg-peer-public-key-file) wg_peer_file=$2 ;;
            --wg-private-key-file) wg_private_file=$2 ;;
            *) die "Unknown prepare option: $1" ;;
        esac
        shift 2
    done
    [ -n "$wan_mac" ] && [ -n "$office_mac" ] && [ -n "$iot_mac" ] && [ -n "$service_mac" ] && [ -n "$office_client" ] && [ -n "$iot_client" ] || die 'Supply all four router MACs and both client MACs'
    seen=' '
    for value in "$wan_mac" "$office_mac" "$iot_mac" "$service_mac" "$office_client" "$iot_client"; do
        case "$seen" in *" $value "*) die 'All router and client MACs must be distinct' ;; esac
        seen="$seen$value "
    done
    check_system
    wan_device=$(find_device "$wan_mac")
    office_device=$(find_device "$office_mac")
    iot_device=$(find_device "$iot_mac")
    service_device=$(find_device "$service_mac")
    wg_enabled=0
    if [ -n "$wg_endpoint$wg_peer_file$wg_private_file" ]; then
        [ -n "$wg_endpoint" ] && [ -n "$wg_peer_file" ] && [ -n "$wg_private_file" ] || die 'WireGuard endpoint and both key files must be supplied together'
        ipv4 "$wg_endpoint"
        need wg
        [ -f /lib/netifd/proto/wireguard.sh ] || die 'Install WireGuard netifd support first'
        [ -r "$wg_peer_file" ] && [ -r "$wg_private_file" ] || die 'WireGuard key file is unreadable'
        wg_peer=$(cat "$wg_peer_file")
        wg_private=$(cat "$wg_private_file")
        for key in "$wg_peer" "$wg_private"; do
            [ "${#key}" = 44 ] && printf '%s\n' "$key" | grep -Eq '^[A-Za-z0-9+/]{43}=$' || die 'Malformed WireGuard key file'
        done
        printf '%s\n' "$wg_private" | wg pubkey >/dev/null 2>&1 || die 'Invalid WireGuard private key'
        wg_enabled=1
    fi
    mkdir -p "$STAGES"
    stage="$STAGES/$(date +%Y%m%d-%H%M%S)-$$"
    mkdir -p "$stage/config" "$stage/backup" "$stage/delta"
    for name in $CONFIGS; do cp "/etc/config/$name" "$stage/backup/$name"; done
    cp "$0" "$stage/virtual-router.sh"
    chmod 700 "$stage/virtual-router.sh"
    cat > "$stage/config/network" <<EOF
config interface 'loopback'
    option device 'lo'
    option proto 'static'
    option ipaddr '127.0.0.1'
    option netmask '255.0.0.0'

config interface 'wan'
    option device '$wan_device'
    option proto 'static'
    option ipaddr '192.168.200.50'
    option netmask '255.255.255.0'
    option gateway '192.168.200.1'
    list dns '192.168.200.1'
    option delegate '0'
EOF
    for spec in "office $office_device 192.168.10.1" "iot $iot_device 192.168.20.1" "service $service_device 192.168.30.1"; do
        set -- $spec; role=$1; device=$2; address=$3
        cat >> "$stage/config/network" <<EOF

config device '${role}_bridge'
    option name 'br-$role'
    option type 'bridge'
    list ports '$device'
    option ipv6 '0'

config interface '$role'
    option device 'br-$role'
    option proto 'static'
    option ipaddr '$address'
    option netmask '255.255.255.0'
    option delegate '0'
EOF
    done
    cat > "$stage/config/dhcp" <<'EOF'
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

config dhcp 'service'
    option interface 'service'
    option ignore '1'
    option ra 'disabled'
    option dhcpv6 'disabled'
    option ndp 'disabled'
EOF
    for spec in "office $office_client 192.168.10.100" "iot $iot_client 192.168.20.100"; do
        set -- $spec; role=$1; client=$2; address=$3
        cat >> "$stage/config/dhcp" <<EOF

config dhcp '$role'
    option interface '$role'
    option start '100'
    option limit '50'
    option leasetime '4h'
    option ra 'disabled'
    option dhcpv6 'disabled'
    option ndp 'disabled'

config host '${role}_client'
    option name '$role-client'
    option mac '$client'
    option ip '$address'
EOF
    done
    cat > "$stage/config/firewall" <<'EOF'
config defaults
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'
    option synflood_protect '1'
    option drop_invalid '1'

config zone 'service'
    option name 'service'
    list network 'service'
    option input 'ACCEPT'
    option output 'ACCEPT'
    option forward 'DROP'

config zone 'office'
    option name 'office'
    list network 'office'
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'

config zone 'iot'
    option name 'iot'
    list network 'iot'
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'

config zone 'wan'
    option name 'wan'
    list network 'wan'
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'
    option masq '1'
    option mtu_fix '1'

config rule 'office_dashboard'
    option name 'Warehouse-Office-dashboard'
    option src 'office'
    option dest 'service'
    option dest_ip '192.168.30.10'
    option proto 'tcp'
    option dest_port '8080'
    option family 'ipv4'
    option target 'ACCEPT'

config rule 'iot_mqtt'
    option name 'Warehouse-IoT-MQTT'
    option src 'iot'
    option dest 'service'
    option dest_ip '192.168.30.10'
    option proto 'tcp'
    option dest_port '1883'
    option family 'ipv4'
    option target 'ACCEPT'
EOF
    for role in office iot; do
        cat >> "$stage/config/firewall" <<EOF

config rule '${role}_dhcp'
    option name 'Warehouse-$role-DHCP'
    option src '$role'
    option proto 'udp'
    option src_port '68'
    option dest_port '67'
    option family 'ipv4'
    option target 'ACCEPT'

config rule '${role}_dns'
    option name 'Warehouse-$role-DNS'
    option src '$role'
    option proto 'tcp udp'
    option dest_port '53'
    option family 'ipv4'
    option target 'ACCEPT'
EOF
    done
    if [ "$wg_enabled" = 1 ]; then
        cat >> "$stage/config/network" <<EOF

config interface 'wg_warehouse'
    option proto 'wireguard'
    option private_key '$wg_private'
    list addresses '10.89.0.2/32'
    option mtu '1420'
    option delegate '0'

config wireguard_wg_warehouse 'warehouse_guangzhou'
    option public_key '$wg_peer'
    option endpoint_host '$wg_endpoint'
    option endpoint_port '51830'
    list allowed_ips '10.89.0.1/32'
    option route_allowed_ips '1'
    option persistent_keepalive '25'
EOF
        cat >> "$stage/config/firewall" <<'EOF'

config zone 'warehouse_vpn'
    option name 'vpn'
    list network 'wg_warehouse'
    option input 'DROP'
    option output 'ACCEPT'
    option forward 'DROP'

config rule 'warehouse_vpn_service'
    option name 'Warehouse-VPN-dashboard-SSH'
    option src 'vpn'
    option src_ip '10.89.0.1'
    option dest 'service'
    option dest_ip '192.168.30.10'
    option proto 'tcp'
    option dest_port '22 8080'
    option family 'ipv4'
    option target 'ACCEPT'
EOF
    fi
    # Zone fall-through otherwise reaches the base chain's DROP policy, which
    # has no packet counter. Explicit final drops preserve the policy and make
    # denied cross-zone probes observable in real fw4 evidence.
    deny_roles='office iot wan'
    [ "$wg_enabled" = 0 ] || deny_roles="$deny_roles vpn"
    for role in $deny_roles; do
        cat >> "$stage/config/firewall" <<EOF

config rule 'warehouse_${role}_forward_deny'
    option name 'Warehouse-$role-forward-deny'
    option src '$role'
    option dest '*'
    option proto 'all'
    option target 'DROP'
EOF
    done
    for name in $CONFIGS; do uci -c "$stage/config" -t "$stage/delta" show "$name" >/dev/null; done
    printf '%s\n' "wan=$wan_device $wan_mac" "office=$office_device $office_mac" "iot=$iot_device $iot_mac" "service=$service_device $service_mac" "office_client=$office_client 192.168.10.100" "iot_client=$iot_client 192.168.20.100" "wireguard_enabled=$wg_enabled" > "$stage/summary.txt"
    touch "$stage/.prepared"
    printf 'Prepared %s\n' "$stage"
    printf 'Review summary and staged configuration locally; then apply from the VM console.\n'
    printf 'Network configuration may contain a WireGuard private key; do not publish it.\n'
}

reload_services() {
    failed=0
    timeout 25 /etc/init.d/network reload || failed=1
    timeout 25 /etc/init.d/dnsmasq restart || failed=1
    if [ -x /etc/init.d/odhcpd ]; then timeout 25 /etc/init.d/odhcpd restart || failed=1; fi
    timeout 25 /etc/init.d/firewall restart || failed=1
    [ "$failed" = 0 ]
}

restore_files() {
    failed=0
    for name in $CONFIGS; do
        cp "$stage/backup/$name" "/etc/config/.$name.warehouse-restore.$$" && mv "/etc/config/.$name.warehouse-restore.$$" "/etc/config/$name" || failed=1
    done
    [ "$failed" = 0 ]
}

rollback() {
    check_stage "$1"
    restore_result=0
    restore_files || restore_result=1
    reload_services || restore_result=1
    if [ "$restore_result" != 0 ]; then
        touch "$stage/.rollback-failed"
        printf 'ERROR: Rollback attempted but a copy/reload failed. Recover from the VMware console using %s/backup.\n' "$stage" >&2
        return 1
    fi
    if [ -f "$ACTIVE" ] && [ "$(cat "$ACTIVE")" = "$stage" ]; then rm -f "$ACTIVE"; fi
    touch "$stage/.rolled-back"
    printf 'Restored original configuration from %s/backup\n' "$stage"
}

apply() {
    check_system
    check_stage "$1"
    for name in $CONFIGS; do
        cmp -s "/etc/config/$name" "$stage/backup/$name" || die "Live $name changed after prepare; prepare a fresh stage"
    done
    mkdir "$LOCK" 2>/dev/null || die "Another apply/rollback may be running; inspect $LOCK from the VM console"
    trap 'result=$?; trap - 0 HUP INT TERM; if [ "$result" -ne 0 ]; then rollback "$stage" || :; fi; rmdir "$LOCK" 2>/dev/null || :; exit "$result"' 0
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # Validate UCI parses before touching /etc/config; stage all replacement files first.
    for name in $CONFIGS; do
        uci -c "$stage/config" -t "$stage/delta" show "$name" >/dev/null
        cp "$stage/config/$name" "/etc/config/.$name.warehouse-new.$$"
    done
    for name in $CONFIGS; do mv "/etc/config/.$name.warehouse-new.$$" "/etc/config/$name"; done
    # fw4 evaluates the staged network/firewall together, before any live reload.
    timeout 15 fw4 check > "$stage/firewall-check.log" 2>&1 || die 'firewall4 preflight failed; restoring original files'
    reload_services || die 'A service reload failed; restoring original configuration'
    touch "$stage/.applied"
    printf '%s\n' "$stage" > "$ACTIVE"
    printf 'Applied virtual warehouse configuration. Check VM console, DHCP and access matrix.\n'
    printf 'Rollback: sh %s rollback %s\n' "$0" "$stage"
}

status() {
    printf '\nIPv4 addresses and routes\n'
    ip -4 -br address
    ip -4 route
    printf '\nLogical interfaces\n'
    for role in wan office iot service wg_warehouse; do
        ubus call "network.interface.$role" status 2>/dev/null || :
    done
    printf '\nFirewall validation\n'
    fw4 check
    printf '\nDHCP leases\n'
    cat /tmp/dhcp.leases 2>/dev/null || :
    if command -v wg >/dev/null 2>&1; then
        printf '\nWireGuard state (private keys are not printed)\n'
        wg show wg_warehouse 2>/dev/null || :
    fi
}

rollback_command() {
    [ "$(id -u)" = 0 ] || die 'Run as root'
    check_stage "$1"
    mkdir "$LOCK" 2>/dev/null || die "Another transaction may be running; inspect $LOCK from the VM console"
    trap 'result=$?; trap - 0; rmdir "$LOCK" 2>/dev/null || :; exit "$result"' 0
    rollback "$stage"
}

case "${1:-}" in
    prepare) shift; prepare "$@" ;;
    apply) [ "$#" = 2 ] || die 'Usage: apply STAGE_DIR'; apply "$2" ;;
    rollback) [ "$#" = 2 ] || die 'Usage: rollback STAGE_DIR'; rollback_command "$2" ;;
    status) status ;;
    *)
        printf '%s\n' 'Usage: sh virtual-router.sh prepare --wan-mac MAC --office-mac MAC --iot-mac MAC --service-mac MAC --office-client-mac MAC --iot-client-mac MAC [--wg-endpoint IPv4 --wg-peer-public-key-file FILE --wg-private-key-file FILE]'
        printf '%s\n' '       sh virtual-router.sh apply STAGE_DIR | rollback STAGE_DIR | status'
        exit 2 ;;
esac
