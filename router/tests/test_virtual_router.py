"""Fault-injection tests; run only inside a disposable Linux container.

The UCI stub validates generated syntax, not OpenWrt/fw4 runtime semantics.
Real fw4, network forwarding and VMware interfaces require VM acceptance.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

if os.environ.get("WAREHOUSE_DISPOSABLE_CONTAINER") != "1" or sys.platform != "linux" or not Path("/.dockerenv").exists():
    raise SystemExit("Run only in a throwaway container with WAREHOUSE_DISPOSABLE_CONTAINER=1")

SCRIPT = Path(__file__).resolve().parents[1] / "virtual-router.sh"
STAGES = Path("/root/warehouse-virtual-stages")
ACTIVE = Path("/tmp/warehouse-virtual-active")
LOCK = Path("/tmp/warehouse-virtual-lock")
FIXTURE = Path("/tmp/warehouse-virtual-tests")
CALLS = Path("/tmp/warehouse-virtual-service-calls")
MACS = [f"02:50:56:11:00:{index:02x}" for index in range(1, 7)]
DEVICES = ["ens9", "eth7", "enp4s0", "ens42"]


def executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


class VirtualRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        for path in (STAGES, LOCK, FIXTURE):
            if path.exists():
                shutil.rmtree(path)
        ACTIVE.unlink(missing_ok=True)
        CALLS.unlink(missing_ok=True)
        FIXTURE.mkdir()
        self.bin = FIXTURE / "bin"
        self.bin.mkdir()
        self.sysnet = FIXTURE / "sysnet"
        for device, mac in zip(DEVICES, MACS):
            p = self.sysnet / device
            (p / "device").mkdir(parents=True)
            (p / "address").write_text(mac + "\n")
        # Bridge with the same MAC must not count as another physical interface.
        bridge = self.sysnet / "br-old"
        bridge.mkdir()
        (bridge / "address").write_text(MACS[0] + "\n")
        Path("/etc/openwrt_release").write_text("DISTRIB_TARGET='x86/64'\n")
        self.original = {}
        for name in ("network", "dhcp", "firewall"):
            p = Path("/etc/config") / name
            p.parent.mkdir(exist_ok=True)
            p.write_text(f"# original-{name}\n")
            self.original[name] = p.read_bytes()
        executable(self.bin / "uci", '''#!/usr/bin/env python3
import os,sys,pathlib,shlex
a=sys.argv[1:]; config='/etc/config'
while a and a[0].startswith('-'):
    option=a.pop(0)
    if option in ('-c','-t'):
        value=a.pop(0)
        if option=='-c': config=value
if a==['changes']:
    print(os.environ.get('TEST_UCI_CHANGES',''),end='');sys.exit(0)
if len(a)==2 and a[0]=='show':
    lines=(pathlib.Path(config)/a[1]).read_text().splitlines()
    in_section=False
    for line in lines:
        parts=shlex.split(line,comments=True)
        if not parts: continue
        if parts[0]=='config' and len(parts) in (2,3): in_section=True
        elif parts[0] in ('list','option') and len(parts)==3 and in_section: pass
        else: sys.exit(1)
    sys.exit(0)
sys.exit(1)
''')
        executable(self.bin / "ubus", '''#!/usr/bin/env python3
import json,os
print(json.dumps({'release':{'target':os.environ.get('TEST_TARGET','x86/64')}}))
''')
        executable(self.bin / "jsonfilter", '''#!/usr/bin/env python3
import json,sys
print(json.load(sys.stdin)['release']['target'])
''')
        executable(self.bin / "ip", "#!/bin/sh\nexit 0\n")
        executable(self.bin / "fw4", "#!/bin/sh\n[ \"${TEST_FAIL_FW4:-0}\" = 0 ]\n")
        executable(self.bin / "wg", "#!/bin/sh\ncat >/dev/null\nprintf 'stub-public-key\\n'\n")
        for service in ("network", "dnsmasq", "odhcpd", "firewall"):
            code = (
                "#!/bin/sh\n"
                f"printf '{service} %s\\n' \"$1\" >> '{CALLS}'\n"
                + ("[ \"${TEST_FAIL_NETWORK:-0}\" = 0 ]\n" if service == "network" else "exit 0\n")
            )
            executable(Path("/etc/init.d") / service, code)
        executable(Path("/lib/netifd/proto/wireguard.sh"), "#!/bin/sh\n")
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", WAREHOUSE_TEST_SYSNET=str(self.sysnet))
        self.args = []
        for option, mac in zip(("wan", "office", "iot", "service", "office-client", "iot-client"), MACS):
            self.args.extend([f"--{option}-mac", mac])

    def run_script(self, *args: str, **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(SCRIPT), *args], env=self.env | environment,
                              text=True, capture_output=True, timeout=15)

    def prepare(self, *extra: str) -> Path:
        result = self.run_script("prepare", *self.args, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        stages = list(STAGES.iterdir())
        self.assertEqual(len(stages), 1)
        return stages[0]

    def assert_original(self) -> None:
        for name, content in self.original.items():
            self.assertEqual((Path("/etc/config") / name).read_bytes(), content)

    def test_prepare_is_nonmutating_and_maps_actual_mac_names(self) -> None:
        stage = self.prepare()
        self.assert_original()
        self.assertFalse(CALLS.exists())
        network = (stage / "config/network").read_text()
        for device in DEVICES:
            self.assertIn(f"'{device}'", network)
        self.assertIn("option gateway '192.168.200.1'", network)
        self.assertIn("option ipaddr '192.168.200.50'", network)
        self.assertNotIn("wireless", network)
        self.assertNotIn("wg_warehouse", network)
        self.assertFalse((stage / "config/wireless").exists())
        firewall = (stage / "config/firewall").read_text()
        self.assertNotIn("config forwarding", firewall)
        self.assertNotIn("51830", firewall)
        self.assertIn("option dest_port '8080'", firewall)
        self.assertIn("option dest_port '1883'", firewall)
        self.assertEqual(firewall.count("option forward 'DROP'"), 5)
        for role in ('office','iot','wan'):
            section = "config rule 'warehouse_"+role+"_forward_deny'"
            self.assertIn(section,firewall)
            self.assertGreater(firewall.index(section),firewall.index("config rule 'iot_mqtt'"))
        self.assertNotIn("warehouse_vpn_forward_deny",firewall)
        dhcp = (stage / "config/dhcp").read_text()
        self.assertIn(MACS[4], dhcp)
        self.assertIn(MACS[5], dhcp)
        self.assertIn("192.168.10.100", dhcp)
        self.assertIn("192.168.20.100", dhcp)

    def test_duplicate_and_multicast_macs_are_rejected(self) -> None:
        for invalid in (MACS[0], "01:50:56:11:00:06", "not-a-mac"):
            args = self.args.copy()
            args[-1] = invalid
            result = self.run_script("prepare", *args)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(STAGES.exists())
            self.assert_original()

    def test_unmapped_mac_is_rejected_before_staging(self) -> None:
        args = self.args.copy()
        args[1] = "02:50:56:22:00:01"
        result = self.run_script("prepare", *args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No hardware interface", result.stderr)
        self.assertFalse(STAGES.exists())

    def test_non_x86_and_pending_edits_are_rejected(self) -> None:
        for env in ({"TEST_TARGET": "ath79/generic"}, {"TEST_UCI_CHANGES": "network.lan.ipaddr='other'"}):
            result = self.run_script("prepare", *self.args, **env)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(STAGES.exists())
            self.assert_original()

    def test_live_edits_after_prepare_are_not_overwritten(self) -> None:
        stage = self.prepare()
        p = Path("/etc/config/network")
        changed = p.read_text() + "# manual edit\n"
        p.write_text(changed)
        result = self.run_script("apply", str(stage))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed after prepare", result.stderr)
        self.assertEqual(p.read_text(), changed)
        self.assertFalse(CALLS.exists())

    def test_firewall_preflight_failure_rolls_back_before_activation(self) -> None:
        stage = self.prepare()
        result = self.run_script("apply", str(stage), TEST_FAIL_FW4="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_original()
        self.assertTrue((stage / ".rolled-back").exists())
        self.assertFalse(LOCK.exists())
        self.assertEqual(CALLS.read_text().splitlines(),
                         ["network reload", "dnsmasq restart", "odhcpd restart", "firewall restart"])

    def test_reload_failure_attempts_every_recovery_service_and_retry_works(self) -> None:
        stage = self.prepare()
        result = self.run_script("apply", str(stage), TEST_FAIL_NETWORK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_original()
        self.assertTrue((stage / ".rollback-failed").exists())
        for service in ("network reload", "dnsmasq restart", "odhcpd restart", "firewall restart"):
            self.assertEqual(CALLS.read_text().splitlines().count(service), 2)
        self.assertFalse(LOCK.exists())
        retry = self.run_script("rollback", str(stage))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue((stage / ".rolled-back").exists())

    def test_successful_apply_and_explicit_restore(self) -> None:
        stage = self.prepare()
        result = self.run_script("apply", str(stage))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((stage / ".applied").exists())
        self.assertEqual(ACTIVE.read_text().strip(), str(stage))
        self.assertFalse(LOCK.exists())
        for name in self.original:
            self.assertEqual((Path("/etc/config") / name).read_bytes(), (stage / "config" / name).read_bytes())
        result = self.run_script("rollback", str(stage))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_original()
        self.assertFalse(ACTIVE.exists())

    def test_optional_wg_is_scoped_and_keys_are_not_printed(self) -> None:
        public = base64.b64encode(bytes(range(32))).decode()
        private = base64.b64encode(bytes(reversed(range(32)))).decode()
        pub_file, private_file = FIXTURE / "peer.pub", FIXTURE / "router.key"
        pub_file.write_text(public + "\n")
        private_file.write_text(private + "\n")
        result = self.run_script("prepare", *self.args, "--wg-endpoint", "203.0.113.5",
                                 "--wg-peer-public-key-file", str(pub_file),
                                 "--wg-private-key-file", str(private_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(private, result.stdout + result.stderr)
        stage = next(STAGES.iterdir())
        network = (stage / "config/network").read_text()
        firewall = (stage / "config/firewall").read_text()
        self.assertIn("list allowed_ips '10.89.0.1/32'", network)
        self.assertNotIn("0.0.0.0/0", network)
        self.assertIn("option src_ip '10.89.0.1'", firewall)
        self.assertIn("option dest_port '22 8080'", firewall)
        self.assertGreater(firewall.index("config rule 'warehouse_vpn_forward_deny'"),firewall.index("config rule 'warehouse_vpn_service'"))
        self.assertNotIn("config forwarding", firewall)
        self.assert_original()
        self.assertFalse(CALLS.exists())

    def test_partial_wg_request_does_not_prepare(self) -> None:
        result = self.run_script("prepare", *self.args, "--wg-endpoint", "203.0.113.5")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(STAGES.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
