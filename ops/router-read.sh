#!/bin/sh
# Fixed Dropbear forced-command; never evaluate client-provided command text.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
[ "$#" = 0 ] || exit 64
[ "${SSH_ORIGINAL_COMMAND:-}" = warehouse-observe-v1 ] || exit 65
set -- ${SSH_CONNECTION:-}
[ "$#" = 4 ] && [ "$1" = 192.168.30.10 ] && [ "$3" = 192.168.30.1 ] || exit 66
printf 'WH_OBSERVER_1\nWAN\n'
ubus call network.interface.wan status
printf '\nROUTES\n'
ip -4 route show
printf '\nHANDSHAKES\n'
wg show wg_warehouse latest-handshakes
printf '\nCOUNTERS_OFFICE\n'
nft -j list chain inet fw4 forward_office
printf '\nCOUNTERS_IOT\n'
nft -j list chain inet fw4 forward_iot
printf '\nCOUNTERS_WAN\n'
nft -j list chain inet fw4 forward_wan
printf '\nEND\n'
