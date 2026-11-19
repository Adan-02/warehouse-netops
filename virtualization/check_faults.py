"""Real COM1-driven fault checks for this isolated VMware lab.

Run explicitly: this entry point stops only the lab broker, or takes down only
the lab router's WAN. A successful live baseline is required before injection.
The finally block always attempts the matching recovery action. Unit tests use
fake consoles and clocks and are not evidence that a live fault was exercised.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

try:
    from .check_matrix import MatrixRunner, ProtocolError, encoded_python, extract_json
except ImportError:
    from check_matrix import MatrixRunner, ProtocolError, encoded_python, extract_json


OFFICE_IP = "192.168.10.100"
CLOUD_IP = "10.89.0.1"
SERVICE_IP = "192.168.30.10"
SERVICE_PORT = 8080
STACK = "cd /opt/warehouse-netops/server && sudo -n docker compose --project-name warehouse-netops "
PUBLIC_FIELDS = (
    "device_id", "broker_connected", "data_status", "demo_mode", "seq",
    "temperature_c", "humidity_pct", "age_seconds", "offline_after_seconds", "last_seen_at",
)


def http_script(prefix, source_ip, handshake=False):
    """Use a freshly bound TCP socket, bypassing all environment proxy settings."""
    script = f"""import http.client,socket,json,time,subprocess
r={{'source_ip':None,'connected':False,'status':None}}
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(4)
try:
 s.bind(({source_ip!r},0));r['source_ip']=s.getsockname()[0]
 s.connect(({SERVICE_IP!r},{SERVICE_PORT}));r['connected']=True
 c=http.client.HTTPConnection({SERVICE_IP!r},{SERVICE_PORT},timeout=4);c.sock=s
 c.request('GET','/api/status',headers={{'Connection':'close'}})
 response=c.getresponse();r['status']=response.status
 value=json.loads(response.read(16384))
 r['data']={{k:value.get(k) for k in {PUBLIC_FIELDS!r}}}
 c.close()
except Exception as e:
 r['error_type']=type(e).__name__
finally:
 s.close()
"""
    if handshake:
        script += """try:
 p=subprocess.run(['sudo','-n','/usr/bin/wg','show','wg-warehouse','latest-handshakes'],capture_output=True,text=True,timeout=4)
 values=[int(line.split()[-1]) for line in p.stdout.splitlines() if len(line.split())==2]
 r['wireguard']={'command_exit':p.returncode,'latest_handshake':max(values,default=0),'observed_at':time.time()}
except Exception as e:
 r['wireguard']={'error_type':type(e).__name__}
"""
    return script + f"print('\\n'+{prefix!r}+json.dumps(r,separators=(',',':')),flush=True)\n"


def online(sample, source_ip=OFFICE_IP):
    value = sample.get("data", {})
    return (
        sample.get("source_ip") == source_ip and sample.get("connected") is True
        and sample.get("status") == 200 and value.get("device_id") == "cabinet-01"
        and value.get("broker_connected") is True and value.get("data_status") == "online"
        and value.get("demo_mode") is True and type(value.get("seq")) is int
        and value["seq"] > 0 and value.get("offline_after_seconds") == 20
    )


def web_alive(sample, source_ip=OFFICE_IP):
    return sample.get("source_ip") == source_ip and sample.get("connected") is True and sample.get("status") == 200


def recent_handshake(sample):
    value = sample.get("wireguard", {})
    stamp = value.get("latest_handshake", 0)
    now = value.get("observed_at", 0)
    return value.get("command_exit") == 0 and isinstance(stamp, (int, float)) and stamp > 0 and 0 <= now - stamp <= 180


def ssh_probe_script(prefix):
    """Read-only reuse of verify_remote's prepared key and COM1 host pin."""
    return f"""import json,os,re,stat,subprocess
from pathlib import Path
r={{'source_ip':'10.89.0.1','target_ip':'192.168.30.10','exit_code':None,'identity_matches':False,'private_paths_guarded':False,'authentication':'publickey','host_key_policy':'strict project pin'}}
try:
 root=Path('/var/lib/warehouse-netops-guangzhou');private=root/'private'
 def guard(path,directory=False):
  info=path.lstat();kind=stat.S_ISDIR if directory else stat.S_ISREG
  if not kind(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=(0o700 if directory else 0o600):
   raise RuntimeError('Unsafe existing project private path')
 for path in (root,private):guard(path,True)
 for path in (root/'.owner',private/'maintenance_ed25519',private/'service_known_hosts'):guard(path)
 if not re.fullmatch('warehouse-netops-guangzhou-v1:[0-9a-f]{{32}}', (root/'.owner').read_text().strip()):
  raise RuntimeError('Existing project ownership marker invalid')
 r['private_paths_guarded']=True
 args=['ssh','-F','/dev/null','-i',str(private/'maintenance_ed25519'),'-b','10.89.0.1',
  '-o','BatchMode=yes','-o','ConnectTimeout=5','-o','ConnectionAttempts=1','-o','StrictHostKeyChecking=yes',
  '-o','UserKnownHostsFile='+str(private/'service_known_hosts'),'-o','GlobalKnownHostsFile=/dev/null',
  '-o','IdentityAgent=none','-o','IdentitiesOnly=yes','-o','PreferredAuthentications=publickey',
  '-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no','-o','HostKeyAlgorithms=ssh-ed25519',
  'warehouse@192.168.30.10','id -u; id -un; hostname']
 p=subprocess.run(args,capture_output=True,text=True,timeout=12)
 r['exit_code']=p.returncode
 r['identity_matches']=p.returncode==0 and p.stdout.strip().splitlines()==['1000','warehouse','warehouse-service']
 if r['identity_matches']:r['identity']={{'uid':1000,'user':'warehouse','hostname':'warehouse-service'}}
except Exception as e:
 r['error_type']=type(e).__name__
print('\\n'+{prefix!r}+json.dumps(r,separators=(',',':')),flush=True)
"""


def ssh_identity_verified(sample):
    return (sample.get('source_ip') == CLOUD_IP and sample.get('target_ip') == SERVICE_IP
            and sample.get('private_paths_guarded') is True and sample.get('exit_code') == 0
            and sample.get('identity_matches') is True
            and sample.get('identity') == {'uid':1000,'user':'warehouse','hostname':'warehouse-service'}
            and sample.get('authentication') == 'publickey' and sample.get('host_key_policy') == 'strict project pin')


class FaultRunner:
    def __init__(self, console_factory, *, cloud_factory=None, sleep=time.sleep,
                 monotonic=time.monotonic, progress=None):
        self.matrix = MatrixRunner(console_factory)
        self.cloud_factory = cloud_factory
        self.cloud_client = None
        self.sleep = sleep
        self.monotonic = monotonic
        self.progress = progress or (lambda message: None)

    def close(self):
        self.matrix.close()
        if self.cloud_client is not None:
            self.cloud_client.close()

    def command(self, role, command, timeout=45):
        result = self.matrix.console(role).command(command, timeout=timeout)
        if result["exit_code"] != 0:
            # Do not export arbitrary serial output or private file contents.
            raise ProtocolError(f"{role} command failed (exit {result['exit_code']}).")
        return result["output"]

    def baseline(self):
        return self.matrix.baseline(require_fresh=True)

    def local_probe(self):
        prefix = "WH_FAULT_" + uuid.uuid4().hex + "_"
        command = encoded_python(http_script(prefix, OFFICE_IP))
        if len(command.encode("utf-8")) > 2900:
            raise ProtocolError("Fault HTTP probe exceeds the serial command limit.")
        return extract_json(self.command("office", command, timeout=15), prefix)

    def cloud_probe(self):
        if self.cloud_factory is None:
            raise ProtocolError("Guangzhou checking was not enabled.")
        if self.cloud_client is None:
            self.cloud_client = self.cloud_factory()
        prefix = "WH_CLOUD_" + uuid.uuid4().hex + "_"
        command = encoded_python(http_script(prefix, CLOUD_IP, handshake=True))
        _, output, _ = self.cloud_client.exec_command(command, timeout=15)
        output.channel.set_combine_stderr(True)
        payload = output.read(16385)
        if len(payload) > 16384 or output.channel.recv_exit_status() != 0:
            raise ProtocolError("Guangzhou read-only HTTP/WireGuard probe failed.")
        return extract_json(payload.decode("utf-8", errors="replace"), prefix)

    def cloud_ssh_probe(self):
        # Called only for broker-stop cloud checks. WAN checks have no
        # dependency on this private key, host pin or service SSH account.
        if self.cloud_factory is None:
            raise ProtocolError('Guangzhou checking was not enabled.')
        if self.cloud_client is None:
            self.cloud_client = self.cloud_factory()
        prefix = 'WH_CLOUD_SSH_' + uuid.uuid4().hex + '_'
        command = 'sudo -n ' + encoded_python(ssh_probe_script(prefix))
        _, output, _ = self.cloud_client.exec_command(command, timeout=20)
        output.channel.set_combine_stderr(True)
        payload = output.read(16385)
        if len(payload) > 16384 or output.channel.recv_exit_status() != 0:
            raise ProtocolError('Guangzhou dedicated-key SSH probe failed.')
        return extract_json(payload.decode('utf-8', errors='replace'), prefix)

    def wan_state(self):
        output = self.command("router", "ubus call network.interface.wan status", timeout=15)
        for start, char in enumerate(output):
            if char != "{":
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(output[start:])
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("up"), bool):
                return {"up": value["up"], "pending": value.get("pending"), "available": value.get("available")}
        raise ProtocolError("Router did not return a complete WAN state.")

    def run(self, phase, *, hold_seconds=27, recovery_seconds=90):
        if phase not in ("broker-stop", "wan-disconnect"):
            raise ValueError("Unknown fault phase")
        if not 25 <= hold_seconds <= 120:
            raise ValueError("Fault hold must be 25 to 120 seconds")
        if not 15 <= recovery_seconds <= 180:
            raise ValueError("Recovery deadline must be 15 to 180 seconds")
        report = {
            "observed_at": datetime.now(timezone.utc).isoformat(), "phase": phase,
            "scope": "VMware wired virtual zones; COM1-driven real fault check",
            "cloud_enabled": self.cloud_factory is not None,
            "us_accessed": False, "checks": [], "samples": [], "actions": [],
            "restoration": {"required": False, "attempted": False, "verified": False},
            "not_verified": ["physical Wi-Fi and ESP32", "authenticated MQTT identity is a separate check"],
        }
        if self.cloud_factory is None:
            report["not_verified"].append("Guangzhou/WireGuard path")
        fault_started = None
        restore_needed = False

        def add(name, passed, evidence=None, *, inconclusive=False):
            report["checks"].append({"name": name, "status": "pass" if passed else "inconclusive" if inconclusive else "failed", "evidence": evidence or {}})
            self.progress(name + ": " + report["checks"][-1]["status"])

        def local(stage):
            sample = self.local_probe()
            report["samples"].append({"stage": stage, "elapsed_seconds": None if fault_started is None else round(self.monotonic() - fault_started, 2), "local": sample})
            return sample

        def cloud(stage):
            sample = self.cloud_probe()
            report["samples"].append({"stage": stage, "elapsed_seconds": None if fault_started is None else round(self.monotonic() - fault_started, 2), "guangzhou": sample})
            return sample

        try:
            self.progress("Collecting live baseline before any fault")
            baseline = self.baseline()
            report["baseline"] = baseline
            add("live_access_matrix_prerequisite", baseline.get("passed") is True, {"status": baseline.get("status")}, inconclusive=True)
            if not baseline.get("passed"):
                return self.finish(report)
            self.command("iot", "systemctl is-active --quiet warehouse-sensor-sim")
            initial = local("baseline")
            add("baseline_local_online", online(initial), initial, inconclusive=True)
            if not online(initial):
                return self.finish(report)
            self.sleep(6)
            before = local("baseline_progression")
            growing = online(before) and before["data"]["seq"] > initial["data"]["seq"]
            add("baseline_sampling_progresses", growing, {"seq_before": initial["data"]["seq"], "seq_after": before.get("data", {}).get("seq")}, inconclusive=True)
            if not growing:
                return self.finish(report)
            if self.cloud_factory is not None:
                cloud_before = cloud("baseline")
                ready = online(cloud_before, CLOUD_IP) and recent_handshake(cloud_before)
                add("baseline_guangzhou_http_and_handshake", ready, cloud_before, inconclusive=True)
                if not ready:
                    return self.finish(report)
                if phase == 'broker-stop':
                    ssh_before = self.cloud_ssh_probe()
                    report['samples'].append({'stage':'baseline','elapsed_seconds':None,'guangzhou_ssh':ssh_before})
                    ssh_ready = ssh_identity_verified(ssh_before)
                    add('baseline_guangzhou_pinned_key_ssh',ssh_ready,ssh_before,inconclusive=True)
                    if not ssh_ready:
                        return self.finish(report)
            if phase == "wan-disconnect":
                before_wan = self.wan_state()
                add("baseline_wan_up", before_wan["up"], before_wan, inconclusive=True)
                if not before_wan["up"]:
                    return self.finish(report)

            # Mark recovery necessary BEFORE issuing the command: a transport
            # timeout is ambiguous and must not leave the injected fault active.
            restore_needed = True
            report["restoration"]["required"] = True
            if phase == "broker-stop":
                action = STACK + "stop --timeout 10 broker"
                role = "service"
            else:
                action, role = "ifdown wan", "router"
            report["actions"].append({"role": role, "command": action, "purpose": "inject"})
            self.command(role, action, timeout=90)
            fault_started = self.monotonic()
            self.progress("Fault injected; observing local service and telemetry")
            if phase == "wan-disconnect":
                state = self.wan_state()
                add("wan_down_confirmed", state["up"] is False, state)
            during = []
            cloud_during = []
            while True:
                during.append(local("fault"))
                if self.monotonic() - fault_started >= hold_seconds:
                    break
                self.sleep(min(5, hold_seconds - (self.monotonic() - fault_started)))
            duration = self.monotonic() - fault_started
            report["fault_observation_seconds"] = round(duration, 2)
            add("fault_held_at_least_25_seconds", duration >= 25, {"seconds": round(duration, 2)})
            add("office_web_survives_fault", all(web_alive(row) for row in during), {"sample_count": len(during)})
            last = during[-1]
            if phase == "broker-stop":
                data = last.get("data", {})
                age = data.get("age_seconds")
                degraded = web_alive(last) and data.get("broker_connected") is False and data.get("data_status") == "stale" and isinstance(age, (int, float)) and age >= 20
                add("broker_fault_is_disconnected_and_stale", degraded, last)
            else:
                continuous = all(online(row) for row in during)
                advancing = continuous and last["data"]["seq"] > before["data"]["seq"]
                add("local_sampling_survives_wan_loss", advancing, {"samples_online": continuous, "seq_before": before["data"]["seq"], "seq_after": last.get("data", {}).get("seq")})
            if self.cloud_factory is not None:
                cloud_during.append(cloud("fault"))
                if phase == "broker-stop":
                    add("guangzhou_web_survives_broker_stop", all(web_alive(row, CLOUD_IP) and recent_handshake(row) for row in cloud_during), cloud_during[-1])
                    ssh_during = self.cloud_ssh_probe()
                    report['samples'].append({'stage':'fault','elapsed_seconds':round(self.monotonic()-fault_started,2),'guangzhou_ssh':ssh_during})
                    add('guangzhou_pinned_key_ssh_survives_broker_stop',ssh_identity_verified(ssh_during),ssh_during)
                else:
                    remote_lost = all(row.get("source_ip") == CLOUD_IP and row.get("connected") is False and row.get("status") is None and row.get("error_type") in ("TimeoutError", "ConnectionRefusedError", "OSError") for row in cloud_during)
                    add("guangzhou_web_lost_with_wan", remote_lost, cloud_during[-1])
        except (Exception, KeyboardInterrupt) as error:
            add("fault_check_execution", False, {"error_type": type(error).__name__, "detail": str(error) if isinstance(error, ProtocolError) else "See the indicated local console; arbitrary error text is not exported."})
        finally:
            if restore_needed:
                report["restoration"]["attempted"] = True
                action = STACK + "start broker" if phase == "broker-stop" else "ifup wan"
                role = "service" if phase == "broker-stop" else "router"
                report["actions"].append({"role": role, "command": action, "purpose": "restore"})
                self.progress("Restoring " + phase)
                restored = False
                for attempt in range(1, 3):
                    try:
                        self.command(role, action, timeout=90)
                        restored = True
                        break
                    except (Exception, KeyboardInterrupt) as error:
                        report["restoration"].setdefault("command_errors", []).append({"attempt": attempt, "error_type": type(error).__name__})
                        self.sleep(2)
                add("restore_command_completed", restored, {"role": role})
                if restored:
                    end = self.monotonic() + recovery_seconds
                    local_recovered = False
                    cloud_recovered = self.cloud_factory is None
                    wan_recovered = phase != "wan-disconnect"
                    recovery_samples = []
                    while True:
                        try:
                            sample = local("recovery")
                            local_recovered = online(sample)
                            recovery_samples.append(sample)
                            if phase == "wan-disconnect":
                                wan_recovered = self.wan_state()["up"] is True
                            if self.cloud_factory is not None:
                                remote = cloud("recovery")
                                cloud_recovered = online(remote, CLOUD_IP) and recent_handshake(remote)
                        except (Exception, KeyboardInterrupt) as error:
                            report["restoration"].setdefault("probe_errors", []).append({"error_type": type(error).__name__})
                        if local_recovered and cloud_recovered and wan_recovered:
                            break
                        remaining = end - self.monotonic()
                        if remaining <= 0:
                            break
                        self.sleep(min(5, remaining))
                    add("restored_local_sampling_online", local_recovered, recovery_samples[-1] if recovery_samples else {})
                    if phase == "wan-disconnect":
                        add("restored_wan_up", wan_recovered)
                    if self.cloud_factory is not None:
                        add("restored_guangzhou_http_and_handshake", cloud_recovered)
                    report["restoration"]["verified"] = local_recovered and cloud_recovered and wan_recovered
                if not report["restoration"]["verified"]:
                    report["restoration"]["manual_recovery"] = {"role": role, "command": action, "transport": "local COM1 console"}
        return self.finish(report)

    @staticmethod
    def finish(report):
        statuses = [item["status"] for item in report["checks"]]
        report["passed"] = bool(statuses) and all(status == "pass" for status in statuses) and (not report["restoration"]["required"] or report["restoration"]["verified"])
        report["status"] = "passed" if report["passed"] else "failed" if "failed" in statuses else "inconclusive"
        return report


def main(argv=None, *, console_factory=None, cloud_factory=None):
    parser = argparse.ArgumentParser(description="故障注入并自动恢复：仅实验 Broker 或 OpenWrt WAN")
    parser.add_argument("--phase", required=True, choices=("broker-stop", "wan-disconnect"))
    parser.add_argument("--guangzhou", action="store_true", help="额外通过已钉住的广州管理 SSH 验证独立 WG 路径")
    parser.add_argument("--hold-seconds", type=float, default=27)
    parser.add_argument("--recovery-seconds", type=float, default=90)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 25 <= args.hold_seconds <= 120 or not 15 <= args.recovery_seconds <= 180:
        parser.error("hold-seconds 必须 25–120，recovery-seconds 必须 15–180")
    if console_factory is None:
        try:
            from .console import Console
        except ImportError:
            from console import Console
        console_factory = Console
    if args.guangzhou:
        if cloud_factory is None:
            try:
                from .cloud_connection import connect_guangzhou
            except ImportError:
                from cloud_connection import connect_guangzhou
            cloud_factory = connect_guangzhou
    else:
        cloud_factory = None
    runner = FaultRunner(console_factory, cloud_factory=cloud_factory,
                         progress=lambda message: print(message, file=sys.stderr, flush=True))
    try:
        report = runner.run(args.phase, hold_seconds=args.hold_seconds, recovery_seconds=args.recovery_seconds)
    finally:
        runner.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"phase": args.phase, "status": report["status"], "passed": report["passed"], "restoration": report["restoration"], "evidence": str(args.output)}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1 if report["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
