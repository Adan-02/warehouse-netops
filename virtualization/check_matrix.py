"""Verify the virtual lab through COM1; no management network is introduced.

Only baseline is implemented. Fault phases deliberately return "unsupported"
without opening a console or changing anything. This checks TCP policy and the
read-only HTTP API, not authenticated MQTT publication or physical networking.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

try:
    from .lab_topology import ADDRESSES,GATEWAYS,PORTS,POLICY,NOT_VERIFIED
    from .probes import (ProtocolError,extract_json,encoded_python,guest_script,
        extract_nft,rules_from_nft,rules_fingerprint,packet_count,targets,
        deny_counter_evidence,network_ready)
    from .lab_session import LabSession
except ImportError:
    from lab_topology import ADDRESSES,GATEWAYS,PORTS,POLICY,NOT_VERIFIED
    from probes import (ProtocolError,extract_json,encoded_python,guest_script,
        extract_nft,rules_from_nft,rules_fingerprint,packet_count,targets,
        deny_counter_evidence,network_ready)
    from lab_session import LabSession


class MatrixRunner(LabSession):
    """Access-policy acceptance scenario; transport lives in LabSession."""
    def baseline(self, require_fresh=False):
        report = new_report("baseline")

        def add(name, status, evidence):
            report["checks"].append({"name": name, "status": status, "evidence": evidence})

        try:
            inventories = {}
            for role in ADDRESSES:
                inventory = self.guest(role, "inventory")
                inventories[role] = inventory
                add(role + "_source_and_single_nic", "pass" if network_ready(role, inventory) else "inconclusive", inventory)
            listening = inventories["service"].get("self_connect", {})
            add("service_ports_listening", "pass" if all(listening.get(str(port)) is True for port in PORTS) else "failed", {"listeners": inventories["service"].get("listeners", []), "self_connect": listening})
            before = self.router()
            add("router_fw4_check", "pass" if before["fw4_check_exit"] == 0 else "failed", {"exit_code": before["fw4_check_exit"], "fingerprint": before["fingerprint"]})
            report["router_initial_rules"] = before["rules"]
            if any(item["status"] != "pass" for item in report["checks"]):
                report["interpretation"] = "Baseline prerequisites are not satisfied; no port-policy conclusion was drawn."
                return finish_report(report)
            for role in POLICY:
                http = {}
                for port, allowed in POLICY[role].items():
                    before = self.router()
                    probes = self.guest(role, 'probe_'+str(port))
                    after = self.router()
                    deny = deny_counter_evidence(before['rules'],after['rules'],role)
                    report.setdefault('counter_evidence',{}).setdefault(role,{})[str(port)]=deny
                    if before['fw4_check_exit'] != 0 or after['fw4_check_exit'] != 0:
                        add(f'{role}_{port}_fw4_check','failed',{'before':before['fw4_check_exit'],'after':after['fw4_check_exit']})
                    row = probes.get("ports", {}).get(str(port), {})
                    if row.get("source_ip") != ADDRESSES[role] or row.get("errno") in (13, 99, 101, 113):
                        status = "inconclusive"
                    elif not isinstance(row.get("connected"), bool):
                        status = "inconclusive"
                    elif row["connected"] != allowed:
                        status = "failed"
                    elif not allowed and not deny["verified"]:
                        status = "inconclusive"
                    else:
                        status = "pass"
                    add(f"{role}_to_service_{port}", status, {"expected_connect": allowed, **row, "deny_counter_verified": deny["verified"] if not allowed else None, "target_listening_verified": listening[str(port)]})
                    if port==8080: http=probes.get('http',{})
                if role == "office":
                    data = http.get("data", {})
                    valid = http.get("status") == 200 and data.get("device_id") == "cabinet-01" and data.get("broker_connected") is True and data.get("demo_mode") is True
                    add("office_dashboard_api", "pass" if valid else "failed", http)
                    report["telemetry_freshness"] = {"observed": data.get("data_status"), "required": require_fresh, "origin_not_authenticated_by_this_check": True}
                    if require_fresh:
                        add("office_telemetry_fresh", "pass" if data.get("data_status") == "online" else "failed", report["telemetry_freshness"])
            report["interpretation"] = "TCP rejection is accepted only alongside verified target listeners, correct source interfaces, stable fw4 rules and increased source-zone-reachable deny counters. Authenticated MQTT publication needs separate evidence."
        except Exception as error:
            add("console_evidence_collection", "inconclusive", {"stage": self.collection_stage, "error_type": type(error).__name__, "detail": str(error) if isinstance(error, ProtocolError) else "Console transport or dependency unavailable.", "action": "Inspect the indicated VM console and dependencies; arbitrary console output is not exported."})
        return finish_report(report)


def new_report(phase):
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "phase": phase, "scope": "VMware wired virtual zones; COM1-driven TCP/HTTP checks", "destination": ADDRESSES["service"], "checks": [], "not_verified": list(NOT_VERIFIED), "mutated_hosts": [], "cloud_or_us_accessed": False}


def finish_report(report):
    statuses = [item["status"] for item in report["checks"]]
    report["passed"] = bool(statuses) and all(value == "pass" for value in statuses)
    report["status"] = "passed" if report["passed"] else "failed" if "failed" in statuses else "inconclusive"
    return report


def main(argv=None, console_factory=None):
    parser = argparse.ArgumentParser(description="通过本地 COM1 控制台验证虚拟仓库访问矩阵")
    parser.add_argument("--phase", choices=("baseline", "broker-stop", "wan-disconnect", "restore"), default="baseline")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-fresh", action="store_true", help="额外要求看板的模拟数据保持 online；不等于验证 MQTT 发布身份")
    args = parser.parse_args(argv)
    if args.phase != "baseline":
        report = new_report(args.phase)
        report.update(status="unsupported", passed=False, interpretation="Fault phases are not implemented. No console was opened and no fault action was attempted.")
        exit_code = 2
    else:
        if console_factory is None:
            try:
                from .console import Console
            except ImportError:
                from console import Console
            console_factory = Console
        runner = MatrixRunner(console_factory)
        try:
            report = runner.baseline(args.require_fresh)
        finally:
            runner.close()
        exit_code = 0 if report["passed"] else 1 if report["status"] == "failed" else 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
