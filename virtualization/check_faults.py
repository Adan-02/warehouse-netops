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

try:
    from .check_matrix import MatrixRunner
    from .lab_session import LabSession
    from .probes import (ProtocolError,encoded_python,extract_json,online,
        web_alive,recent_handshake,ssh_probe_script,ssh_identity_verified)
    from . import probes as _probes
    from .lab_topology import OFFICE_IP,CLOUD_IP,SERVICE_IP,SERVICE_PORT,STACK,PUBLIC_FIELDS
except ImportError:
    from check_matrix import MatrixRunner
    from lab_session import LabSession
    from probes import (ProtocolError,encoded_python,extract_json,online,
        web_alive,recent_handshake,ssh_probe_script,ssh_identity_verified)
    import probes as _probes
    from lab_topology import OFFICE_IP,CLOUD_IP,SERVICE_IP,SERVICE_PORT,STACK,PUBLIC_FIELDS


def http_script(prefix,source_ip,handshake=False):
    """Compatibility wrapper retaining the existing test override points."""
    return _probes.http_script(prefix,source_ip,handshake,service_ip=SERVICE_IP,service_port=SERVICE_PORT)


class FaultRunner:
    def __init__(self, console_factory, *, cloud_factory=None, sleep=time.sleep,
                 monotonic=time.monotonic, progress=None):
        self.session=LabSession(console_factory,cloud_factory=cloud_factory)
        self.matrix=self.session  # compatibility for existing scenario fakes
        self.cloud_factory=cloud_factory
        self.sleep=sleep
        self.monotonic=monotonic
        self.progress=progress or (lambda message:None)

    def close(self):self.session.close()

    def command(self,role,command,timeout=45):
        return self.session.command(role,command,timeout)

    def baseline(self):
        return MatrixRunner.baseline(self.session,require_fresh=True)

    def local_probe(self):return self.session.local_probe()

    def cloud_probe(self):return self.session.cloud_probe()

    def cloud_ssh_probe(self):return self.session.cloud_ssh_probe()

    def wan_state(self):return self.session.wan_state()

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
