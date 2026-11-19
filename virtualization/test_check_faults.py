"""No VMs, SSH clients, cloud endpoints, or real fault commands are used here."""
import contextlib
import http.server
import io
import json
import os
from pathlib import PurePosixPath
import stat
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

try:
    from . import check_faults as faults
except ImportError:
    import check_faults as faults


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("negative wait")
        self.now += seconds

    def monotonic(self):
        return self.now


class FakeRunner(faults.FaultRunner):
    def __init__(self, cloud=False):
        self.clock = FakeClock()
        super().__init__(lambda role: None, cloud_factory=(lambda: None) if cloud else None,
                         sleep=self.clock.sleep, monotonic=self.clock.monotonic)
        self.baseline_passes = True
        self.actions = []
        self.fault = None
        self.stopped_at = None
        self.fail_injection = False
        self.fail_restore_once = False
        self.fail_all_restore = False
        self.never_recovers = False
        self.raise_during_fault = False
        self.cloud_baseline_bad = False
        self.cloud_fault_wrong_source = False
        self.cloud_ssh_bad_baseline = False
        self.cloud_ssh_bad_during = False
        self.cloud_ssh_calls = []
        self.restore_attempts = 0
        self.inject_count = 0
        self.probe_count = 0

    def baseline(self):
        return {"passed": self.baseline_passes, "status": "passed" if self.baseline_passes else "inconclusive"}

    def command(self, role, command, timeout=45):
        self.actions.append((role, command))
        if command == "systemctl is-active --quiet warehouse-sensor-sim":
            return ""
        if "stop --timeout 10 broker" in command or command == "ifdown wan":
            self.fault = "broker-stop" if "broker" in command else "wan-disconnect"
            self.inject_count += 1
            self.stopped_at = self.clock.now
            if self.fail_injection:
                raise TimeoutError("private secret must not be exported")
        elif "start broker" in command or command == "ifup wan":
            self.restore_attempts += 1
            if self.fail_all_restore or (self.fail_restore_once and self.restore_attempts == 1):
                raise TimeoutError("private secret must not be exported")
            self.fault = None
        return ""

    def wan_state(self):
        return {"up": self.fault != "wan-disconnect", "pending": False, "available": True}

    def sample(self, source_ip):
        elapsed = 0 if self.stopped_at is None else self.clock.now - self.stopped_at
        broker_down = self.fault == "broker-stop"
        seq = 101 if broker_down else 100 + int(self.clock.now / 5)
        return {
            "source_ip": source_ip, "connected": True, "status": 200,
            "data": {
                "device_id": "cabinet-01", "broker_connected": not broker_down,
                "data_status": "stale" if broker_down and elapsed >= 20 else "online",
                "demo_mode": True, "seq": seq,
                "age_seconds": elapsed if broker_down else 0.1,
                "offline_after_seconds": 20,
            },
        }

    def local_probe(self):
        self.probe_count += 1
        if self.fault and self.raise_during_fault:
            raise RuntimeError("private secret must not be exported")
        sample = self.sample(faults.OFFICE_IP)
        if self.restore_attempts and self.never_recovers:
            sample["data"]["broker_connected"] = False
        return sample

    def cloud_probe(self):
        sample = self.sample(faults.CLOUD_IP)
        sample["wireguard"] = {"command_exit": 0, "latest_handshake": 1000, "observed_at": 1000 + self.clock.now}
        if self.cloud_baseline_bad and not self.inject_count:
            sample["status"] = 403
        if self.fault == "wan-disconnect":
            sample.update(connected=False, status=None, error_type="TimeoutError")
            sample.pop("data")
            if self.cloud_fault_wrong_source:
                sample["source_ip"] = "10.77.0.1"
        return sample

    def cloud_ssh_probe(self):
        self.cloud_ssh_calls.append('baseline' if self.fault is None else 'fault')
        sample = {'source_ip':faults.CLOUD_IP,'target_ip':faults.SERVICE_IP,'exit_code':0,'identity_matches':True,
                  'private_paths_guarded':True,'authentication':'publickey','host_key_policy':'strict project pin',
                  'identity':{'uid':1000,'user':'warehouse','hostname':'warehouse-service'}}
        if (not self.inject_count and self.cloud_ssh_bad_baseline) or (self.fault and self.cloud_ssh_bad_during):
            sample['identity_matches'] = False
        return sample


class FaultTests(unittest.TestCase):
    def test_broker_stop_stale_and_restored(self):
        runner = FakeRunner()
        report = runner.run("broker-stop")
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["fault_observation_seconds"], 25)
        self.assertTrue(report["restoration"]["verified"])
        final_fault = [row["local"] for row in report["samples"] if row["stage"] == "fault"][-1]
        self.assertEqual(final_fault["data"]["data_status"], "stale")
        self.assertFalse(final_fault["data"]["broker_connected"])
        self.assertIn(("service", faults.STACK + "start broker"), runner.actions)
        self.assertIsNone(runner.fault)

    def test_wan_loss_keeps_sampling_and_restores(self):
        runner = FakeRunner()
        report = runner.run("wan-disconnect")
        self.assertTrue(report["passed"])
        self.assertIn(("router", "ifdown wan"), runner.actions)
        self.assertIn(("router", "ifup wan"), runner.actions)
        rows = [row["local"]["data"]["seq"] for row in report["samples"] if row["stage"] == "fault"]
        self.assertGreater(rows[-1], rows[0])

    def test_baseline_failure_never_injects(self):
        runner = FakeRunner()
        runner.baseline_passes = False
        report = runner.run("broker-stop")
        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["restoration"]["required"])
        self.assertEqual(runner.actions, [])

    def test_no_progression_never_injects(self):
        runner = FakeRunner()
        original = runner.local_probe

        def frozen():
            sample = original()
            sample["data"]["seq"] = 100
            return sample

        runner.local_probe = frozen
        report = runner.run("wan-disconnect")
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(runner.inject_count, 0)

    def test_ambiguous_injection_timeout_still_restores(self):
        runner = FakeRunner()
        runner.fail_injection = True
        report = runner.run("broker-stop")
        self.assertFalse(report["passed"])
        self.assertEqual(runner.restore_attempts, 1)
        self.assertTrue(report["restoration"]["verified"])
        self.assertNotIn("private secret", json.dumps(report))

    def test_probe_exception_still_restores(self):
        runner = FakeRunner()
        runner.raise_during_fault = True
        report = runner.run("wan-disconnect")
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["restoration"]["verified"])
        self.assertIsNone(runner.fault)
        self.assertNotIn("private secret", json.dumps(report))

    def test_restore_retry_is_targeted(self):
        runner = FakeRunner()
        runner.fail_restore_once = True
        report = runner.run("broker-stop")
        self.assertTrue(report["passed"])
        self.assertEqual(runner.restore_attempts, 2)
        self.assertEqual(report["restoration"]["command_errors"], [{"attempt": 1, "error_type": "TimeoutError"}])

    def test_unrecovered_fault_fails_with_bounded_wait(self):
        runner = FakeRunner()
        runner.never_recovers = True
        report = runner.run("broker-stop", recovery_seconds=15)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["restoration"]["verified"])
        self.assertLessEqual(runner.clock.now, 6 + 27 + 15)

    def test_failed_restoration_reports_exact_targeted_manual_command(self):
        runner = FakeRunner()
        runner.fail_all_restore = True
        report = runner.run("wan-disconnect")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(runner.restore_attempts, 2)
        self.assertFalse(report["restoration"]["verified"])
        self.assertEqual(report["restoration"]["manual_recovery"], {"role": "router", "command": "ifup wan", "transport": "local COM1 console"})

    def test_keyboard_interrupt_during_fault_always_restores(self):
        runner = FakeRunner()
        original = runner.local_probe

        def interrupted():
            if runner.fault:
                raise KeyboardInterrupt()
            return original()

        runner.local_probe = interrupted
        report = runner.run("broker-stop")
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["restoration"]["verified"])
        self.assertIsNone(runner.fault)

    def test_guangzhou_wan_transition_requires_baseline_and_recovery(self):
        runner = FakeRunner(cloud=True)
        report = runner.run("wan-disconnect")
        self.assertTrue(report["passed"])
        self.assertFalse(report["us_accessed"])
        self.assertTrue(report["cloud_enabled"])
        self.assertTrue(report["restoration"]["verified"])
        stages = {row["stage"] for row in report["samples"] if "guangzhou" in row}
        self.assertEqual(stages, {"baseline", "fault", "recovery"})

    def test_guangzhou_broker_fault_keeps_web_and_wg(self):
        runner = FakeRunner(cloud=True)
        report = runner.run("broker-stop")
        self.assertTrue(report["passed"])
        self.assertEqual(runner.cloud_ssh_calls,['baseline','fault'])

    def test_broker_failed_ssh_baseline_does_not_inject(self):
        runner = FakeRunner(cloud=True)
        runner.cloud_ssh_bad_baseline = True
        report = runner.run('broker-stop')
        self.assertEqual(report['status'],'inconclusive')
        self.assertEqual(runner.inject_count,0)
        self.assertEqual(runner.cloud_ssh_calls,['baseline'])

    def test_broker_lost_ssh_during_fault_fails_and_restores(self):
        runner = FakeRunner(cloud=True)
        runner.cloud_ssh_bad_during = True
        report = runner.run('broker-stop')
        self.assertEqual(report['status'],'failed')
        self.assertTrue(report['restoration']['verified'])
        self.assertEqual(runner.cloud_ssh_calls,['baseline','fault'])

    def test_wan_and_local_broker_never_use_ssh_probe(self):
        for phase,cloud in (('wan-disconnect',True),('wan-disconnect',False),('broker-stop',False)):
            runner = FakeRunner(cloud=cloud)
            with mock.patch.object(runner,'cloud_ssh_probe',side_effect=AssertionError('SSH dependency forbidden')):
                self.assertTrue(runner.run(phase)['passed'])

    def test_ssh_identity_requires_exact_host_user_uid_and_pin_guard(self):
        sample = FakeRunner(cloud=True).cloud_ssh_probe()
        self.assertTrue(faults.ssh_identity_verified(sample))
        for changes in ({'private_paths_guarded':False},{'source_ip':'10.77.0.1'},{'exit_code':255},
                        {'identity':{'uid':0,'user':'root','hostname':'warehouse-service'}},
                        {'identity':{'uid':1000,'user':'warehouse','hostname':'different-service'}},
                        {'host_key_policy':'accept-new'}):
            self.assertFalse(faults.ssh_identity_verified({**sample,**changes}))

    def test_ssh_script_reuses_guarded_key_and_strict_pin_without_creation(self):
        calls = []

        class FakePath:
            def __init__(self,path):
                self.path = PurePosixPath(path)

            def __truediv__(self,name):
                return FakePath(self.path/name)

            def __str__(self):
                return str(self.path)

            def lstat(self):
                directory = self.path.name in ('warehouse-netops-guangzhou','private')
                return SimpleNamespace(st_uid=0,st_mode=(stat.S_IFDIR|0o700) if directory else (stat.S_IFREG|0o600))

            def read_text(self):
                if self.path.name != '.owner':
                    raise AssertionError('Never read private key or host-pin content into Python')
                return 'warehouse-netops-guangzhou-v1:'+'a'*32+'\n'

        def run(args,**kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0,stdout='1000\nwarehouse\nwarehouse-service\n',stderr='')

        script = faults.ssh_probe_script('WH_SSH_TEST_')
        compile(script,'<ssh-fault-test>','exec')
        with mock.patch('pathlib.Path',FakePath),mock.patch('subprocess.run',run),contextlib.redirect_stdout(io.StringIO()) as output:
            exec(script,{})
        sample = faults.extract_json(output.getvalue(),'WH_SSH_TEST_')
        self.assertTrue(faults.ssh_identity_verified(sample))
        self.assertEqual(len(calls),1)
        args = calls[0]
        self.assertEqual(args[:3],['ssh','-F','/dev/null'])
        self.assertIn('/var/lib/warehouse-netops-guangzhou/private/maintenance_ed25519',args)
        self.assertEqual(args[args.index('-b')+1],'10.89.0.1')
        for option in ('BatchMode=yes','StrictHostKeyChecking=yes','UserKnownHostsFile=/var/lib/warehouse-netops-guangzhou/private/service_known_hosts',
                       'GlobalKnownHostsFile=/dev/null','IdentityAgent=none','IdentitiesOnly=yes','PreferredAuthentications=publickey',
                       'PasswordAuthentication=no','KbdInteractiveAuthentication=no','HostKeyAlgorithms=ssh-ed25519'):
            self.assertIn(option,args)
        self.assertEqual(args[-2:],['warehouse@192.168.30.10','id -u; id -un; hostname'])
        self.assertNotIn('ssh-keygen',script)
        self.assertNotIn('authorized_keys',script)

    def test_ssh_script_never_exports_unexpected_remote_output(self):
        script = faults.ssh_probe_script('WH_SSH_BAD_')
        # Execute only the public-result path with safe fake path guards. An
        # unsafe missing path must not invoke SSH or expose arbitrary errors.
        with mock.patch('pathlib.Path.lstat',side_effect=RuntimeError('private secret')),mock.patch('subprocess.run') as run,contextlib.redirect_stdout(io.StringIO()) as output:
            exec(script,{})
        sample = faults.extract_json(output.getvalue(),'WH_SSH_BAD_')
        self.assertFalse(sample['identity_matches'])
        self.assertFalse(sample['private_paths_guarded'])
        self.assertNotIn('private secret',json.dumps(sample))
        run.assert_not_called()

    def test_guangzhou_failed_baseline_never_injects(self):
        runner = FakeRunner(cloud=True)
        runner.cloud_baseline_bad = True
        report = runner.run("wan-disconnect")
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(runner.inject_count, 0)

    def test_wrong_guangzhou_source_does_not_prove_wan_failure(self):
        runner = FakeRunner(cloud=True)
        runner.cloud_fault_wrong_source = True
        report = runner.run("wan-disconnect")
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["restoration"]["verified"])

    def test_hold_limit_enforced_before_any_console(self):
        runner = FakeRunner()
        for duration in (0, 20, 24.99, 121):
            with self.assertRaises(ValueError):
                runner.run("broker-stop", hold_seconds=duration)
        self.assertEqual(runner.actions, [])

    def test_one_connection_per_role_and_close(self):
        made = []

        class FakeConsole:
            def __init__(self, role):
                self.role = role
                self.closed = False
                made.append(self)

            def command(self, command, timeout=45):
                return {"exit_code": 0, "output": "ok"}

            def close(self):
                self.closed = True

        runner = faults.FaultRunner(FakeConsole)
        runner.command("office", "id")
        runner.command("office", "id")
        runner.command("router", "id")
        runner.close()
        self.assertEqual([item.role for item in made], ["office", "router"])
        self.assertTrue(all(item.closed for item in made))

    def test_probe_fits_console_and_binds_source_without_proxy(self):
        # This is a local loopback HTTP server, never the actual lab endpoint.
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"device_id": "cabinet-01", "seq": 9, "private_field": "omitted"}).encode())

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(faults, "SERVICE_IP", "127.0.0.1"), mock.patch.object(faults, "SERVICE_PORT", server.server_port):
                script = faults.http_script("WH_TEST_", "127.0.0.1")
            self.assertLess(len(faults.encoded_python(script).encode()), 2900)
            with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}), contextlib.redirect_stdout(io.StringIO()) as output:
                exec(compile(script, "<fault-probe-test>", "exec"), {})
            value = faults.extract_json(output.getvalue(), "WH_TEST_")
            self.assertEqual(value["source_ip"], "127.0.0.1")
            self.assertEqual(value["status"], 200)
            self.assertEqual(value["data"]["seq"], 9)
            self.assertNotIn("private_field", value["data"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
