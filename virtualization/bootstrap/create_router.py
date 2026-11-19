"""Prepare only the lab router disk; edits a copy of its ext4 filesystem offline."""
import gzip
import json
import pathlib
import struct
import subprocess
from virtualization.runtime import MACS, PRIVATE, prepare_dirs, tool, vmx, ensure_stopped, load_paths
from virtualization.lab_topology import ROUTER_WAN_IP, NAT_GATEWAY


def main(argv=None):
    from virtualization.bootstrap.guards import parse_bootstrap, require_staging
    parse_bootstrap(__doc__, argv)
    require_staging()

    ensure_stopped('router')
    prepare_dirs()
    key = PRIVATE / "lab_ed25519"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    work = load_paths().vms / "router"
    work.mkdir(exist_ok=True)
    raw = work / "router.raw"
    disk = work / "router.vmdk"
    if disk.exists():
        raise SystemExit("Router disk already exists; refusing to overwrite a configured lab VM")
    with gzip.open(load_paths().images / "openwrt-25.12.6-x86-64-generic-ext4-combined.img.gz", "rb") as source:
        raw.write_bytes(source.read())
    with raw.open("rb") as f:
        mbr = f.read(512)
        entry = mbr[462:478]
        start, count = struct.unpack_from("<II", entry, 8)
        if entry[4] != 0x83 or start == 0 or count == 0:
            raise ValueError("Unexpected root partition; refusing filesystem editing")
        f.seek(start * 512)
        (work / "rootfs.ext4").write_bytes(f.read(count * 512))
    (work / "authorized_keys").write_text(key.with_suffix(".pub").read_text(), encoding="utf-8")
    bootstrap = """#!/bin/sh
set -eu
wan=''
for path in /sys/class/net/*; do
    [ "$(cat "$path/address")" = '@@WAN_MAC@@' ] && wan="${path##*/}" || true
done
[ -n "$wan" ]
uci -q delete network.wan || true
uci -q delete network.wan6 || true
uci -q delete network.lan || true
uci -q delete network.@device[0] || true
uci set network.lan=interface
uci set network.lan.proto=static
uci set network.lan.device="$wan"
uci set network.lan.ipaddr='@@WAN_IP@@'
uci set network.lan.netmask='255.255.255.0'
uci set network.lan.gateway='@@NAT_GATEWAY@@'
uci set network.lan.dns='@@NAT_GATEWAY@@'
uci set dhcp.lan.ignore='1'
uci set dropbear.@dropbear[0].PasswordAuth='off'
uci set dropbear.@dropbear[0].RootPasswordAuth='off'
uci commit
echo bootstrap-ready > /etc/warehouse-bootstrap
exit 0
"""
    bootstrap = bootstrap.replace('@@WAN_MAC@@', MACS['router'][0]).replace('@@WAN_IP@@', ROUTER_WAN_IP).replace('@@NAT_GATEWAY@@', NAT_GATEWAY)
    (work / "bootstrap.sh").write_text(bootstrap, encoding="utf-8", newline="\n")
    commands = """mkdir /etc/dropbear
write /work/vms/router/authorized_keys /etc/dropbear/authorized_keys
set_inode_field /etc/dropbear/authorized_keys mode 0100600
write /work/vms/router/bootstrap.sh /etc/uci-defaults/99-warehouse-bootstrap
set_inode_field /etc/uci-defaults/99-warehouse-bootstrap mode 0100755
"""
    (work / "debugfs.commands").write_text(commands, encoding="utf-8")
    result = tool("debugfs", "-w", "-f", "/work/vms/router/debugfs.commands", "/work/vms/router/rootfs.ext4")
    if "write:" in result.stderr or "File not found" in result.stderr:
        raise RuntimeError(result.stderr)
    with raw.open("r+b") as f:
        f.seek(start * 512)
        f.write((work / "rootfs.ext4").read_bytes())
    tool("qemu-img", "convert", "-f", "raw", "-O", "vmdk", "-o", "subformat=monolithicSparse,adapter_type=ide",
         "/work/vms/router/router.raw", "/work/vms/router/router.vmdk")
    target = vmx("router", disk)
    print(json.dumps({"vmx": str(target), "staging_ip": ROUTER_WAN_IP, "macs": MACS["router"]}, indent=2))


if __name__ == "__main__":
    main()
