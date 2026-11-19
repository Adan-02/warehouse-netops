"""Read-only probe programs and evidence parsers, shared by all scenarios.

Importing this module neither reads machine configuration nor opens a session.
"""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import re
import zlib
try:
    from .lab_topology import ADDRESSES,GATEWAYS,PORTS,OFFICE_IP,CLOUD_IP,SERVICE_IP,SERVICE_PORT,PUBLIC_FIELDS
    from .transport_limits import command_limit
except ImportError:
    from lab_topology import ADDRESSES,GATEWAYS,PORTS,OFFICE_IP,CLOUD_IP,SERVICE_IP,SERVICE_PORT,PUBLIC_FIELDS
    from transport_limits import command_limit

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


def encoded_python(script: str, *, role=None) -> str:
    source = script.encode('utf-8')
    data = base64.b64encode(source).decode("ascii")
    command='python3 -c "import base64;exec(base64.b64decode(\'' + data + '\'))"'
    compressed = base64.b64encode(zlib.compress(source, level=9)).decode('ascii')
    compressed_command = 'python3 -c "import base64,zlib;exec(zlib.decompress(base64.b64decode(\'' + compressed + '\')))"'
    if len(compressed_command) < len(command):
        command = compressed_command
    if role is not None and len(command.encode('utf-8'))>command_limit(role):
        raise ProtocolError('Guest command exceeds the shared serial line limit.')
    return command


def remote_sources(*names):
    """Read complete probe modules; transport encoding is handled separately."""
    allowed = {'guest_inventory', 'service_listeners', 'guest_ports', 'office_status',
        'http_status', 'ssh_identity', 'application_health'}
    if any(name not in allowed for name in names):
        raise ValueError('Unknown remote probe program')
    directory = Path(__file__).with_name('remote_probes')
    return '\n'.join((directory / (name + '.py')).read_text(encoding='utf-8') for name in names)


def probe_parameters(parameters):
    return 'import json\nparameters=json.loads(' + repr(json.dumps(parameters, separators=(',', ':'))) + ')\n'


def probe_output(prefix, variable='result'):
    # A fresh line excludes interactive-shell bracketed-paste control bytes.
    return "print('\\n'+" + repr(prefix) + "+json.dumps(" + variable + ",separators=(',',':')),flush=True)\n"


def guest_script(role: str, purpose: str, prefix: str) -> str:
    parameters = {'role': role, 'purpose': purpose, 'source_ip': ADDRESSES[role],
        'service_ip': SERVICE_IP, 'ports': PORTS}
    if purpose == 'inventory':
        names, calls = ['guest_inventory'], ['inventory(parameters,result)']
        if role == 'service':
            names.append('service_listeners')
            calls.append('service_listeners(parameters,result)')
    elif purpose == 'probes' or purpose.startswith('probe_'):
        selected = PORTS if purpose == 'probes' else (int(purpose.split('_', 1)[1]),)
        if any(port not in PORTS for port in selected):
            raise ValueError('Unsupported probe port')
        parameters['ports'] = selected
        names, calls = ['guest_ports'], ['guest_ports(parameters,result)']
        if role == 'office' and (purpose == 'probes' or selected == (8080,)):
            names.append('office_status')
            calls.append('office_status(parameters,result)')
            parameters['http_fields'] = ('device_id', 'broker_connected', 'data_status', 'demo_mode',
                'seq', 'temperature_c', 'humidity_pct', 'age_seconds')
    else:
        raise ValueError('Unknown guest script purpose')
    source = remote_sources(*names) + '\n' + probe_parameters(parameters)
    source += "result={'role':parameters['role'],'purpose':parameters['purpose']}\ntry:\n"
    source += ''.join(' ' + call + '\n' for call in calls)
    return source + "except Exception as exc:\n result['error_type']=type(exc).__name__\n" + probe_output(prefix)


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


def http_script(prefix, source_ip, handshake=False, *, service_ip=SERVICE_IP, service_port=SERVICE_PORT):
    """Use a freshly bound TCP socket, bypassing all environment proxy settings."""
    parameters = {'source_ip': source_ip, 'service_ip': service_ip, 'service_port': service_port,
        'public_fields': PUBLIC_FIELDS, 'handshake': handshake}
    return (remote_sources('http_status') + '\n' + probe_parameters(parameters)
        + 'result=http_status(parameters)\n' + probe_output(prefix))


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
    parameters = {'source_ip': CLOUD_IP, 'service_ip': SERVICE_IP}
    return (remote_sources('ssh_identity') + '\n' + probe_parameters(parameters)
        + 'result=ssh_identity(parameters)\n' + probe_output(prefix))


def ssh_identity_verified(sample):
    return (sample.get('source_ip') == CLOUD_IP and sample.get('target_ip') == SERVICE_IP
            and sample.get('private_paths_guarded') is True and sample.get('exit_code') == 0
            and sample.get('identity_matches') is True
            and sample.get('identity') == {'uid':1000,'user':'warehouse','hostname':'warehouse-service'}
            and sample.get('authentication') == 'publickey' and sample.get('host_key_policy') == 'strict project pin')
