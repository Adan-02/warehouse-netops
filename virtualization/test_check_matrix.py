"""Pure unit tests; no named pipe, VM, broker or cloud is contacted."""

from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("check_matrix", Path(__file__).with_name("check_matrix.py"))
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


def rules(office=0, iot=0):
    return [
        {"chain": "forward_office", "handle": 1, "comment": "office default drop", "expr": [{"counter": {"packets": office, "bytes": office * 60}}, {"drop": None}]},
        {"chain": "forward_iot", "handle": 2, "comment": "iot default drop", "expr": [{"counter": {"packets": iot, "bytes": iot * 60}}, {"drop": None}]},
    ]


class FakeRunner(matrix.MatrixRunner):
    def __init__(self, *, bad_source=False, no_counter=False, denied_connect=False, listener_missing=False):
        super().__init__(lambda role: None)
        self.index = 0
        self.bad_source, self.no_counter = bad_source, no_counter
        self.denied_connect, self.listener_missing = denied_connect, listener_missing
        self.probed = []

    def guest(self, role, purpose):
        if purpose == "inventory":
            data = {"role": role, "nics": [{"name": "ens33", "up": True, "addresses": [matrix.ADDRESSES[role] + "/24"]}], "routes": [{"dst": "default", "gateway": matrix.GATEWAYS[role], "dev": "ens33"}]}
            if role == "iot" and self.bad_source:
                data["nics"].append({"name": "ens34", "up": True, "addresses": ["192.168.200.61/24"]})
            if role == "service":
                data["listeners"] = ["LISTEN known services"]
                data["self_connect"] = {str(port): not (self.listener_missing and port == 22) for port in matrix.PORTS}
            return data
        self.probed.append(role)
        data = {"ports": {str(port): {"connected": allowed or (self.denied_connect and port == 22), "source_ip": matrix.ADDRESSES[role], "errno": None if allowed else 110} for port, allowed in matrix.POLICY[role].items()}}
        if role == "office":
            data["http"] = {"status": 200, "data": {"device_id": "cabinet-01", "broker_connected": True, "demo_mode": True, "data_status": "online"}}
        return data

    def router(self):
        snapshot = rules(office=0 if self.no_counter else self.probed.count('office')*2, iot=0 if self.no_counter else self.probed.count('iot')*2)
        self.index += 1
        return {"fw4_check_exit": 0, "rules": snapshot, "fingerprint": matrix.rules_fingerprint(snapshot)}


class MatrixTests(unittest.TestCase):
    def test_json_ignores_serial_echo_and_trailing_prompt(self):
        prefix = "WH_MATRIX_unique_"
        output = "root@vm:~# python3 -c 'encoded command'\r\n" + prefix + '{"ok":true}\r\nroot@vm:~# '
        self.assertEqual(matrix.extract_json(output, prefix), {"ok": True})
        with self.assertRaises(matrix.ProtocolError):
            matrix.extract_json(output + "\n" + prefix + '{}\n', prefix)

    def test_serial_line_size_and_scripts_compile(self):
        for role in matrix.ADDRESSES:
            for purpose in ("inventory", "probes", 'probe_22','probe_1883','probe_8080'):
                code = matrix.guest_script(role, purpose, "WH_MATRIX_" + "a" * 32 + "_")
                compile(code, "<guest>", "exec")
                self.assertLess(len(matrix.encoded_python(code).encode()), 3900)

    def test_nft_parsing_ignores_trailing_console_text(self):
        data = {"nftables": [{"rule": {"family": "inet", "table": "fw4", **rules()[0]}}]}
        self.assertEqual(matrix.rules_from_nft(matrix.extract_nft("echo command\n" + json.dumps(data) + "\nroot# ")), [rules()[0]])

    def test_correct_source_and_only_one_nic_required(self):
        inventory = FakeRunner().guest("office", "inventory")
        self.assertTrue(matrix.network_ready("office", inventory))
        inventory["nics"][0]["addresses"].append("192.168.200.60/24")
        self.assertFalse(matrix.network_ready("office", inventory))

    def test_deny_counter_must_match_zone_and_stable_rules(self):
        self.assertTrue(matrix.deny_counter_evidence(rules(), rules(office=2), "office")["verified"])
        self.assertFalse(matrix.deny_counter_evidence(rules(), rules(iot=2), "office")["verified"])
        changed = rules(office=2)
        changed[0]["expr"].append({"accept": None})
        self.assertEqual(matrix.deny_counter_evidence(rules(), changed, "office")["reason"], "rules_changed_during_probe")
        self.assertEqual(matrix.deny_counter_evidence(rules(office=3), rules(office=1), "office")["reason"], "counter_reset_during_probe")

    def test_baseline_pass_requires_all_evidence(self):
        report = FakeRunner().baseline(require_fresh=True)
        self.assertTrue(report["passed"])
        self.assertFalse(report["cloud_or_us_accessed"])
        self.assertIn("authenticated MQTT publish from IoT", report["not_verified"])

    def test_deny_counter_in_reachable_leaf_chain(self):
        before = [
            {"chain": "forward_office", "handle": 3, "comment": None, "expr": [{"jump": {"target": "drop_from_office"}}]},
            {"chain": "drop_from_office", "handle": 4, "comment": None, "expr": [{"counter": {"packets": 0}}, {"drop": None}]},
        ]
        after = copy.deepcopy(before)
        after[1]["expr"][0]["counter"]["packets"] = 2
        result = matrix.deny_counter_evidence(before, after, "office")
        self.assertTrue(result["verified"])
        self.assertEqual(result["deltas"][0]["chain"], "drop_from_office")
        self.assertFalse(matrix.deny_counter_evidence(before, after, "iot")["verified"])

    def test_rejected_socket_without_counter_is_inconclusive(self):
        report = FakeRunner(no_counter=True).baseline()
        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "inconclusive")

    def test_wrong_source_skips_policy_probes(self):
        runner = FakeRunner(bad_source=True)
        report = runner.baseline()
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(runner.probed, [])

    def test_missing_listener_fails_prerequisite(self):
        runner = FakeRunner(listener_missing=True)
        self.assertEqual(runner.baseline()["status"], "failed")
        self.assertEqual(runner.probed, [])

    def test_unexpected_allowed_connection_is_failed(self):
        self.assertEqual(FakeRunner(denied_connect=True).baseline()["status"], "failed")

    def test_fault_phases_are_explicitly_unsupported_and_do_not_open_console(self):
        def forbidden(role):
            self.fail("Unsupported phases must not open consoles")
        for phase in ("broker-stop", "wan-disconnect", "restore"):
            output = io.StringIO()
            with redirect_stdout(output):
                result = matrix.main(["--phase", phase], forbidden)
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "unsupported")


if __name__ == "__main__":
    unittest.main()
