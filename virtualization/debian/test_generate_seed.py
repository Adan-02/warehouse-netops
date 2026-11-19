"""Contract checks for guest seeds; these never configure the host network."""

import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("generate_seed", Path(__file__).with_name("generate_seed.py"))
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)
PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(b"x" * 51).decode("ascii")


class SeedTests(unittest.TestCase):
    def test_all_roles_have_static_mac_matched_staging_network_and_locked_ssh(self):
        for role, info in seed.ROLES.items():
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                seed.generate_seed(role, root, PUBLIC_KEY, "warehouse-" + role, "00:50:56:30:00:10")
                for generated in root.iterdir():
                    self.assertNotIn(b"\r", generated.read_bytes(), generated.name)
                net = json.loads((root / "network-config").read_text())
                nic = net["ethernets"]["warehouse"]
                self.assertEqual(nic["addresses"], [info["staging_ip"] + "/24"])
                self.assertFalse(nic["dhcp4"])
                self.assertEqual(nic["match"]["macaddress"], "00:50:56:30:00:10")
                user = json.loads((root / "user-data").read_text().split("\n", 1)[1])
                self.assertFalse(user["ssh_pwauth"])
                self.assertTrue(user["disable_root"])
                self.assertTrue(user["users"][0]["lock_passwd"])
                self.assertNotIn("passwd", user["users"][0])
                self.assertEqual(user["runcmd"], [["/usr/local/sbin/warehouse-bootstrap", "--origin", "cloud-init"]])
                self.assertEqual(json.loads((root / "meta-data").read_text())["instance-id"], "warehouse-" + role)

    def test_final_network_is_dhcp_only_for_client_roles(self):
        for role in seed.ROLES:
            config = seed.networkd_config(role, "00:50:56:30:00:10", "final")
            self.assertIn("MACAddress=00:50:56:30:00:10", config)
            if role == "service":
                self.assertIn("Address=192.168.30.10/24", config)
                self.assertIn("Gateway=192.168.30.1", config)
                self.assertNotIn("DHCP=ipv4", config)
            else:
                self.assertIn("DHCP=ipv4", config)
                self.assertNotIn("192.168.200.", config)

    def test_switch_requires_readiness_and_never_restarts_live_network(self):
        script = seed.switch_script("service", "00:50:56:30:00:10")
        self.assertIn('== "ready"', script)
        self.assertIn("prepared_for_next_boot", script)
        self.assertIn("network: {config: disabled}", script)
        self.assertNotIn("systemctl restart", script)
        self.assertNotIn("ifdown", script)
        self.assertIn("/sys/class/dmi/id/sys_vendor", script)

    def test_bootstrap_failure_does_not_write_ready_unconditionally(self):
        script = seed.bootstrap_script("service")
        self.assertIn("set -eu", script)
        self.assertIn("trap finish EXIT", script)
        self.assertIn('"status":"failed"', script)
        self.assertIn("docker.io docker-cli docker-compose", script)
        self.assertLess(script.index("docker compose version"), script.index("'status':'ready'"))

    def test_office_autologin_is_local_and_ssh_stays_locked(self):
        config = seed.cloud_config("office", "00:50:56:30:00:10", PUBLIC_KEY)
        files = {item["path"]: item["content"] for item in config["write_files"]}
        self.assertIn("autologin-user=warehouse", files["/etc/lightdm/lightdm.conf.d/50-warehouse-lab.conf"])
        self.assertIn("PasswordAuthentication no", files["/etc/ssh/sshd_config.d/10-warehouse-lab.conf"])

    def test_serial_console_is_local_autologin_and_preserves_ssh_policy(self):
        for role in seed.ROLES:
            config = seed.cloud_config(role, "00:50:56:30:00:10", PUBLIC_KEY)
            files = {item["path"]: item["content"] for item in config["write_files"]}
            self.assertIn("--autologin warehouse", files["/etc/systemd/system/serial-getty@ttyS0.service.d/warehouse-autologin.conf"])
            self.assertIn("console=ttyS0,115200n8", files["/etc/default/grub.d/99-warehouse-serial.cfg"])
            script = files["/usr/local/sbin/warehouse-bootstrap"]
            self.assertLess(script.index("serial-getty@ttyS0"), script.index("apt-get"))

    def test_invalid_mac_and_instance_ids_are_rejected(self):
        for mac in ["", "not-a-mac", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"]:
            with self.subTest(mac=mac), self.assertRaises(ValueError):
                seed.normalize_mac(mac)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            seed.generate_seed("iot", Path(tmp), PUBLIC_KEY, "id\ninvalid", "00:50:56:30:00:10")

    def test_only_public_key_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nredacted")
            with self.assertRaises(ValueError):
                seed.read_public_key(key)
            key.write_text(PUBLIC_KEY + " personal-comment")
            self.assertEqual(seed.read_public_key(key), PUBLIC_KEY)


if __name__ == "__main__":
    unittest.main()
