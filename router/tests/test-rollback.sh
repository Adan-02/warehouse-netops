#!/bin/sh
# Run only in a throwaway Linux container. Uses its /etc/config and /root.
set -eu
[ "${WAREHOUSE_DISPOSABLE_CONTAINER:-}" = 1 ] || {
    echo 'Set WAREHOUSE_DISPOSABLE_CONTAINER=1 only inside a disposable container.' >&2
    exit 2
}
umask 077
script=${1:-/router/lab-router.sh}
stage=/root/warehouse-netops-stages/test-rollback
active=/tmp/warehouse-netops-active
mkdir -p "$stage/backup" "$stage/config" /etc/config /etc/init.d /tmp/testbin "$active"
cp "$script" "$stage/lab-router.sh"
touch "$stage/.prepared"
for name in network wireless dhcp firewall; do
    printf 'original-%s\n' "$name" > "$stage/backup/$name"
    printf 'changed-%s\n' "$name" > "$stage/config/$name"
    cp "$stage/config/$name" "/etc/config/$name"
done
printf '%s\n' "$stage" > "$active/stage"
cat > /etc/init.d/network <<'EOF'
#!/bin/sh
echo network >> /tmp/test-service-calls
test "${FAIL_NETWORK:-0}" = 0
EOF
for name in dnsmasq odhcpd firewall; do
    printf '#!/bin/sh\necho %s >> /tmp/test-service-calls\n' "$name" > "/etc/init.d/$name"
done
chmod 700 /etc/init.d/network /etc/init.d/dnsmasq /etc/init.d/odhcpd /etc/init.d/firewall
if FAIL_NETWORK=1 sh "$script" rollback "$stage" >/tmp/test-output 2>&1; then
    echo 'A failed network reload must not claim successful rollback.' >&2
    exit 1
fi
for name in network wireless dhcp firewall; do cmp "/etc/config/$name" "$stage/backup/$name"; done
for name in network dnsmasq odhcpd firewall; do grep -qx "$name" /tmp/test-service-calls; done
test -f "$stage/.rollback-failed"
test -f "$active/stage"
sh "$script" rollback "$stage" >/tmp/test-output 2>&1
test -f "$stage/.rolled-back"
test ! -d "$active"

# A firewall syntax error during activation must trigger automatic rollback.
mkdir "$active"
printf '%s\n' "$stage" > "$active/stage"
cat > /tmp/testbin/fw4 <<'EOF'
#!/bin/sh
exit 1
EOF
chmod 700 /tmp/testbin/fw4
if PATH="/tmp/testbin:$PATH" sh "$script" activate "$stage" >/tmp/test-output 2>&1; then
    echo 'Invalid firewall configuration must fail activation.' >&2
    exit 1
fi
for name in network wireless dhcp firewall; do cmp "/etc/config/$name" "$stage/backup/$name"; done
test ! -d "$active"
printf 'PASS: failed reload reports failure and attempts every service; retry restores; firewall validation failure rolls back.\n'
