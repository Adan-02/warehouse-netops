"""Verify the virtual lab through COM1; no management network is introduced.

Only baseline is implemented. Fault phases deliberately return "unsupported"
without opening a console or changing anything. This checks TCP policy and the
read-only HTTP API, not authenticated MQTT publication or physical networking.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

ADDRESSES = {"office": "192.168.10.100", "iot": "192.168.20.100", "service": "192.168.30.10"}
GATEWAYS = {"office": "192.168.10.1", "iot": "192.168.20.1", "service": "192.168.30.1"}
PORTS = (22, 1883, 8080)
POLICY = {"office": {22: False, 1883: False, 8080: True}, "iot": {22: False, 1883: True, 8080: False}}
NOT_VERIFIED = ["authenticated MQTT publish from IoT", "WireGuard/Guangzhou", "WAN-disconnect fault", "broker-stop fault", "Office-to-IoT client isolation", "physical Wi-Fi and ESP32"]


class ProtocolError(Exception):
    """A safe diagnostic: never include arbitrary serial output or credentials."""


def extract_json(output: str, prefix: str) -> dict:
    matches = re.findall(r"(?:^|\r?\n)" + re.escape(prefix) + r"([^\r\n]+)", output)
    if len(matches) != 1:
        raise ProtocolError("Expected exactly one completed JSON result from the VM.")
    try:
        value = json.loads(matches[0])
    except (ValueError, TypeError):
        raise ProtocolError("VM JSON result is incomplete or invalid.") from None
    if not isinstance(value, dict):
        raise ProtocolError("VM JSON result must be an object.")
    return value


def encoded_python(script: str) -> str:
    data = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return 'python3 -c "import base64;exec(base64.b64decode(\'' + data + '\'))"'


def guest_script(role: str, purpose: str, prefix: str) -> str:
    address = ADDRESSES[role]
    common = f"""import json,subprocess,socket,pathlib,time,urllib.request
prefix={prefix!r}
result={{'role':{role!r},'purpose':{purpose!r}}}
try:
"""
    if purpose == "inventory":
        body = f""" links=json.loads(subprocess.check_output(['ip','-j','-4','addr','show'],text=True))
 nics=[]
 for item in links:
  name=item['ifname']
  if pathlib.Path('/sys/class/net/'+name+'/device').exists():
   nics.append({{'name':name,'addresses':[a['local']+'/'+str(a['prefixlen']) for a in item.get('addr_info',[]) if a.get('family')=='inet'],'up':'UP' in item.get('flags',[])}})
 result['nics']=nics
 result['routes']=json.loads(subprocess.check_output(['ip','-j','-4','route','show'],text=True))
"""
        if role == "service":
            body += """ result['listeners']=[s for s in subprocess.check_output(['ss','-H','-ltn'],text=True).splitlines() if any(s.split()[3].endswith(':'+str(p)) for p in (22,1883,8080))]
 result['self_connect']={}
 for port in (22,1883,8080):
  try:
   with socket.create_connection(('192.168.30.10',port),timeout=2,source_address=('192.168.30.10',0)):
    result['self_connect'][str(port)]=True
  except OSError:
   result['self_connect'][str(port)]=False
"""
    elif purpose == "probes" or purpose.startswith('probe_'):
        selected = PORTS if purpose=='probes' else (int(purpose.split('_',1)[1]),)
        if any(port not in PORTS for port in selected): raise ValueError('Unsupported probe port')
        body = f""" result['ports']={{}}
 for port in {selected!r}:
  s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(2)
  row={{'connected':False,'source_ip':None,'errno':None}};start=time.monotonic()
  try:
   s.bind(({address!r},0));row['source_ip']=s.getsockname()[0]
   s.connect(('192.168.30.10',port));row['connected']=True
  except OSError as exc:
   row['errno']=exc.errno;row['error_type']=type(exc).__name__
  finally:
   s.close()
  row['elapsed_ms']=round((time.monotonic()-start)*1000,1)
  result['ports'][str(port)]=row
"""
        if role == "office" and (purpose=='probes' or selected==(8080,)):
            body += """ try:
  opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
  with opener.open('http://192.168.30.10:8080/api/status',timeout=4) as response:
   value=json.load(response)
   result['http']={'status':response.status,'data':{k:value.get(k) for k in ('device_id','broker_connected','data_status','demo_mode','seq','temperature_c','humidity_pct','age_seconds')}}
 except Exception as exc:
  result['http']={'error_type':type(exc).__name__}
"""
    else:
        raise ValueError("Unknown guest script purpose")
    # Interactive bash emits bracketed-paste control bytes before the first
    # output character. A fresh line keeps the protocol prefix unambiguous.
    return common + body + "except Exception as exc:\n result['error_type']=type(exc).__name__\nprint('\\n'+prefix+json.dumps(result,separators=(',',':')),flush=True)\n"


def extract_nft(output: str) -> dict:
    for start in re.finditer(r'\{\s*"nftables"\s*:', output):
        try:
            value, _ = json.JSONDecoder().raw_decode(output[start.start():])
        except ValueError:
            continue
        if isinstance(value.get("nftables"), list):
            return value
    raise ProtocolError("No complete nftables JSON was returned by the router.")


def rules_from_nft(nft: dict) -> list[dict]:
    rules = []
    for item in nft["nftables"]:
        if "rule" in item:
            rule = item["rule"]
            if rule.get("family") == "inet" and rule.get("table") == "fw4":
                rules.append({k: rule.get(k) for k in ("chain", "handle", "comment", "expr")})
    return rules


def rules_fingerprint(rules: list[dict]) -> str:
    clean = []
    for rule in rules:
        clean.append({**rule, "expr": [{"counter": {}} if "counter" in item else item for item in rule.get("expr") or []]})
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def packet_count(rule: dict) -> int:
    return sum(item["counter"].get("packets", 0) for item in rule.get("expr") or [] if isinstance(item.get("counter"), dict))


def targets(rule: dict) -> list[str]:
    return [item[action]["target"] for item in rule.get("expr") or [] for action in ("jump", "goto") if isinstance(item.get(action), dict) and "target" in item[action]]


def deny_counter_evidence(before: list[dict], after: list[dict], role: str) -> dict:
    """Only count deny verdicts reachable from this source zone's forward chain."""
    if rules_fingerprint(before) != rules_fingerprint(after):
        return {"verified": False, "reason": "rules_changed_during_probe", "deltas": []}
    chains: dict[str, list[dict]] = {}
    for rule in after:
        chains.setdefault(rule["chain"], []).append(rule)

    def only_deny(chain, visited):
        if chain in visited or chain not in chains:
            return False
        outcomes = []
        for rule in chains[chain]:
            for item in rule.get("expr") or []:
                if "accept" in item or "return" in item:
                    return False
                if "drop" in item or "reject" in item:
                    outcomes.append(True)
                for action in ("jump", "goto"):
                    if action in item:
                        outcomes.append(only_deny(item[action].get("target"), visited | {chain}))
        return bool(outcomes) and all(outcomes)

    reachable = set()
    pending = ["forward_" + role]
    while pending:
        chain = pending.pop()
        if chain in reachable:
            continue
        reachable.add(chain)
        for rule in chains.get(chain, []):
            pending.extend(targets(rule))
    prior = {(item["chain"], item["handle"]): item for item in before}
    deltas = []
    for rule in after:
        if rule["chain"] not in reachable:
            continue
        expressions = rule.get("expr") or []
        deny = any("drop" in item or "reject" in item for item in expressions) or any(only_deny(target, set()) for target in targets(rule))
        if not deny:
            continue
        old = prior[(rule["chain"], rule["handle"])]
        delta = packet_count(rule) - packet_count(old)
        if delta < 0:
            return {"verified": False, "reason": "counter_reset_during_probe", "deltas": []}
        if delta:
            deltas.append({"chain": rule["chain"], "handle": rule["handle"], "packets_before": packet_count(old), "packets_after": packet_count(rule), "delta": delta, "comment": rule["comment"], "expr": rule["expr"]})
    return {"verified": bool(deltas), "reason": "zone_reachable_deny_counter_increased" if deltas else "no_correlated_deny_counter", "deltas": deltas}


def network_ready(role: str, inventory: dict) -> bool:
    nics = inventory.get("nics", [])
    if len(nics) != 1 or not nics[0].get("up") or nics[0].get("addresses") != [ADDRESSES[role] + "/24"]:
        return False
    defaults = [item for item in inventory.get("routes", []) if item.get("dst") == "default"]
    return len(defaults) == 1 and defaults[0].get("gateway") == GATEWAYS[role] and defaults[0].get("dev") == nics[0]["name"]


class MatrixRunner:
    def __init__(self, console_factory):
        self.console_factory = console_factory
        self.consoles = {}
        self.collection_stage = "not_started"

    def console(self, role):
        if role not in self.consoles:
            self.consoles[role] = self.console_factory(role)
        return self.consoles[role]

    def close(self):
        for console in self.consoles.values():
            try:
                console.close()
            except Exception:
                pass

    def guest(self, role, purpose):
        self.collection_stage = role + "_" + purpose
        prefix = "WH_MATRIX_" + uuid.uuid4().hex + "_"
        command = encoded_python(guest_script(role, purpose, prefix))
        if len(command.encode()) > 3900:
            raise ProtocolError("Guest command would exceed the conservative serial line limit.")
        result = self.console(role).command(command, timeout=35)
        if result["exit_code"] != 0:
            raise ProtocolError("Guest Python check failed; inspect the VM locally.")
        data = extract_json(result["output"], prefix)
        if data.get("error_type"):
            raise ProtocolError("Guest inventory or probe dependencies are unavailable.")
        return data

    def router(self):
        self.collection_stage = "router_fw4_nft"
        prefix = "WH_FW4_" + uuid.uuid4().hex
        command = "fw4 check >/dev/null 2>&1; wh_fw_rc=$?; printf '\\n" + prefix + ":%s\\n' \"$wh_fw_rc\"; nft -a -j list table inet fw4"
        result = self.console("router").command(command, timeout=35)
        matches = re.findall(r"(?:^|\r?\n)" + re.escape(prefix) + r":(\d+)(?:\r?\n)", result["output"])
        if result["exit_code"] != 0 or len(matches) != 1:
            raise ProtocolError("Router did not return complete fw4/nft evidence.")
        rules = rules_from_nft(extract_nft(result["output"]))
        return {"fw4_check_exit": int(matches[0]), "fingerprint": rules_fingerprint(rules), "rules": rules}

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
