"""Identity-path and Office/IoT isolation checks on the real wired VM lab.

Identity collection is read-only. Isolation creates only two private, uniquely
named, short-lived TCP-9000 listeners and always stops them in finally. Nothing
changes firewall policy, long-running services, cloud servers, or credentials.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import sys
import time
import uuid
import zlib
import base64

try:
    from .lab_session import LabSession
    from .probes import ProtocolError,deny_counter_evidence,extract_json,network_ready
    from .lab_topology import ADDRESSES
    from .remote_probes.iot_identity import public_payload,puback_records
    from .remote_probes.broker_identity import broker_connections
    from .transport_limits import command_limit
except ImportError:
    from lab_session import LabSession
    from probes import ProtocolError,deny_counter_evidence,extract_json,network_ready
    from lab_topology import ADDRESSES
    from remote_probes.iot_identity import public_payload,puback_records
    from remote_probes.broker_identity import broker_connections
    from transport_limits import command_limit


PORT = 9000


class StopCollection(Exception):
    """A failed prerequisite already has a structured report entry."""








def compressed_python(script):
    payload = base64.b64encode(zlib.compress(script.encode("utf-8"), level=9)).decode("ascii")
    command = 'python3 -c "import base64,zlib;exec(zlib.decompress(base64.b64decode(\'' + payload + '\')))"'
    if len(command.encode("utf-8")) > command_limit('office')-100:
        raise ProtocolError("Path-check command exceeds the conservative serial line limit.")
    return command



def remote_program(name,parameters):
    """Read a named source program; data parameters never become shell syntax."""
    if name not in ('iot_identity','broker_identity'):
        raise ValueError('Unknown identity probe program')
    code=(Path(__file__).with_name('remote_probes')/(name+'.py')).read_text(encoding='utf-8')
    data=json.dumps(parameters,separators=(',',':'))
    return code+'\nimport json\nr=probe(json.loads('+repr(data)+'))\n'


def iot_script():
    return remote_program('iot_identity',{'iot_ip':ADDRESSES['iot'],
        'service_ip':ADDRESSES['service'],'publisher_unit':'warehouse-sensor-sim'})


def broker_script():
    return remote_program('broker_identity',{'server_directory':'/opt/warehouse-netops/server'})


def office_script():
    return """import http.client,socket,json
r={'source_ip':None,'connected':False,'status':None}
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(4)
try:
 s.bind(('192.168.10.100',0));r['source_ip']=s.getsockname()[0]
 s.connect(('192.168.30.10',8080));r['connected']=True
 c=http.client.HTTPConnection('192.168.30.10',8080,timeout=4);c.sock=s
 c.request('GET','/api/status',headers={'Connection':'close'});response=c.getresponse();r['status']=response.status
 value=json.loads(response.read(16384));r['data']={k:value.get(k) for k in ('device_id','broker_connected','data_status','demo_mode','seq','temperature_c','humidity_pct','age_seconds')};c.close()
except Exception as e: r['error_type']=type(e).__name__
finally: s.close()
"""


def socket_script(source, target):
    # These are fixed role addresses, never user-supplied scan targets.
    if source not in (ADDRESSES['office'], ADDRESSES['iot']) or target not in (ADDRESSES['office'], ADDRESSES['iot']):
        raise ValueError("Only the two lab client endpoints may be probed")
    return f"""import socket,time
r={{'source_ip':None,'target_ip':{target!r},'port':{PORT},'connected':False,'errno':None}}
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(3);start=time.monotonic()
try:
 s.bind(({source!r},0));r['source_ip']=s.getsockname()[0];s.connect(({target!r},{PORT}));r['connected']=True
except OSError as e: r['errno']=e.errno;r['error_type']=type(e).__name__
finally: s.close()
r['elapsed_ms']=round((time.monotonic()-start)*1000,1)
"""


def listener_code(address):
    if address not in (ADDRESSES['office'], ADDRESSES['iot']):
        raise ValueError("Listener address must be a lab client endpoint")
    # Repeated checks may leave accepted sockets in TIME_WAIT. REUSEADDR
    # permits a clean rerun, while no REUSEPORT or policy rules are installed.
    return f"import socket\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(({address!r},{PORT}));s.listen(8)\nwhile True:\n c,_=s.accept();c.close()\n"


def unit_state_script(unit, address, stop=False):
    if not re.fullmatch(r"warehouse-path-listener-[0-9a-f]{12}-(?:office|iot)", unit):
        raise ValueError("Unexpected transient unit name")
    prelude = f"import subprocess\nunit={unit!r}\nr={{}}\n"
    if stop:
        prelude += """p=subprocess.run(['sudo','-n','systemctl','stop',unit],capture_output=True,text=True,timeout=15);r['stop_exit']=p.returncode
subprocess.run(['sudo','-n','systemctl','reset-failed',unit],capture_output=True,text=True,timeout=5)
"""
    return prelude + f"""p=subprocess.run(['systemctl','show',unit,'--property=LoadState,ActiveState,SubState,MainPID'],capture_output=True,text=True)
state=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
r.update({{'load_state':state.get('LoadState'),'active_state':state.get('ActiveState'),'sub_state':state.get('SubState'),'main_pid':int(state.get('MainPID','0'))}})
p=subprocess.run(['ss','-H','-ltn'],capture_output=True,text=True)
r['port_listening']=any(len(line.split())>=4 and line.split()[3].rsplit(':',1)[-1]=={str(PORT)!r} for line in p.stdout.splitlines())
r['address_listening']=any(len(line.split())>=4 and line.split()[3]=={address+':'+str(PORT)!r} for line in p.stdout.splitlines());r['ss_exit']=p.returncode
"""


def classify_deny(row, source, target_listening, deny):
    if row.get("source_ip") != source or not target_listening or not isinstance(row.get("connected"), bool):
        return "inconclusive"
    if row["connected"]:
        return "failed"
    # ICMP rejects may surface as EHOSTUNREACH (113). A known listener,
    # correct NIC/default route and correlated nft deny counter are required.
    if row.get("errno") in (13, 99, 101) or not deny.get("verified"):
        return "inconclusive"
    return "pass"


class PathsRunner:
    def __init__(self, console_factory, *, sleep=time.sleep, progress=None):
        self.session = LabSession(console_factory)
        self.sleep = sleep
        self.progress = progress or (lambda message: None)

    @property
    def matrix(self):
        """Compatibility for older local harnesses; the dependency is a session."""
        return self.session

    @matrix.setter
    def matrix(self,value):self.session=value

    def close(self):
        self.session.close()

    def command(self, role, command, timeout=30):
        result = self.session.console(role).command(command, timeout=timeout)
        if result['exit_code']:
            raise ProtocolError(f"{role} diagnostic failed (exit {result['exit_code']}).")
        return result['output']

    def guest_json(self, role, script, timeout=30):
        prefix = "WH_PATH_" + uuid.uuid4().hex + "_"
        code = script + f"\nimport json\nprint('\\n'+{prefix!r}+json.dumps(r,separators=(',',':')),flush=True)\n"
        return extract_json(self.command(role, compressed_python(code), timeout), prefix)

    @staticmethod
    def report(phase):
        return {"observed_at": datetime.now(timezone.utc).isoformat(), "phase": phase,
                "scope": "VMware wired lab; COM1; fixed endpoints only", "checks": [],
                "cloud_or_us_accessed": False, "not_verified": ["physical Wi-Fi and ESP32"],
                "temporary_units": [], "cleanup": []}

    def add(self, report, name, status, evidence):
        report['checks'].append({'name': name, 'status': status, 'evidence': evidence})
        self.progress(name + ': ' + status)

    @staticmethod
    def finish(report):
        states = [item['status'] for item in report['checks']]
        report['passed'] = bool(states) and all(state == 'pass' for state in states)
        report['status'] = 'passed' if report['passed'] else 'failed' if 'failed' in states else 'inconclusive'
        return report

    def identity(self):
        report = self.report('identity')
        try:
            inventory = self.session.guest('iot', 'inventory')
            ready = network_ready('iot', inventory)
            self.add(report, 'iot_single_nic_and_gateway', 'pass' if ready else 'inconclusive', inventory)
            if not ready:
                return self.finish(report)
            first = self.guest_json('iot', iot_script())
            api = self.guest_json('office', office_script())
            last = self.guest_json('iot', iot_script())
            broker = self.guest_json('service', broker_script())
            stable = first.get('main_pid') == last.get('main_pid') and last.get('main_pid', 0) > 0
            running = stable and last.get('unit_active') is True and last.get('source_argument_matches') is True and last.get('publisher_script_matches') is True and bool(last.get('mqtt_sockets'))
            self.add(report, 'iot_current_publisher_and_mqtt_socket', 'pass' if running else 'inconclusive', {key: last.get(key) for key in ('main_pid','unit_active','source_argument_matches','publisher_script_matches','mqtt_sockets')})
            samples = (first.get('pubacks', []) if stable else []) + last.get('pubacks', [])
            self.add(report, 'iot_current_process_recent_puback', 'pass' if samples and last.get('journal_exit') == 0 else 'inconclusive', {'samples': samples, 'journal_exit': last.get('journal_exit'), 'freshness_window_seconds': 20})
            ports = {row.get('source_port') for row in last.get('mqtt_sockets', []) if row.get('source_ip') == ADDRESSES['iot'] and row.get('peer_ip') == ADDRESSES['service'] and row.get('peer_port') == 1883 and row.get('main_pid_matches') is True}
            matching = [row for row in broker.get('connections', []) if row.get('source_ip') == ADDRESSES['iot'] and row.get('source_port') in ports and row.get('username') == 'cabinet-01']
            auth = bool(matching) and broker.get('command_exit') == 0 and running
            self.add(report, 'broker_authentication_matches_live_iot_source', 'pass' if auth else 'inconclusive', {**broker, 'matching_connections': matching, 'interpretation': 'Missing/unknown/expired connection notices cannot prove the authenticated source; no credentials were read.'})
            value = api.get('data', {})
            fresh = api.get('source_ip') == ADDRESSES['office'] and api.get('connected') is True and api.get('status') == 200 and value.get('broker_connected') is True and value.get('data_status') == 'online' and value.get('demo_mode') is True
            payload = public_payload(value)
            matched = payload is not None and any(all(sample.get(key) == item for key, item in payload.items()) for sample in samples)
            self.add(report, 'office_fresh_api_matches_iot_puback_payload', 'pass' if fresh and matched else 'inconclusive', {'api': api, 'exact_seq_temperature_humidity_match': matched})
        except (Exception, KeyboardInterrupt) as error:
            self.add(report, 'identity_collection', 'inconclusive', {'error_type': type(error).__name__, 'detail': str(error) if isinstance(error, ProtocolError) else 'Inspect the indicated local VM; arbitrary error text is not exported.'})
        return self.finish(report)

    def isolation(self):
        report = self.report('client-isolation')
        attempted = []
        nonce = uuid.uuid4().hex[:12]
        try:
            for role in ('office', 'iot'):
                inventory = self.session.guest(role, 'inventory')
                ready = network_ready(role, inventory)
                self.add(report, role+'_single_nic_and_gateway', 'pass' if ready else 'inconclusive', inventory)
                if not ready:
                    raise StopCollection()
            initial = self.session.router()
            self.add(report, 'router_fw4_check_before_listeners', 'pass' if initial['fw4_check_exit'] == 0 else 'failed', {'fingerprint': initial['fingerprint']})
            if initial['fw4_check_exit']:
                raise StopCollection()
            ready_targets = {}
            units = {}
            for role in ('office', 'iot'):
                address = ADDRESSES[role]
                unit = 'warehouse-path-listener-' + nonce + '-' + role
                guard = self.guest_json(role, unit_state_script(unit, address))
                unused = guard.get('load_state') == 'not-found' and guard.get('active_state') == 'inactive' and guard.get('ss_exit') == 0 and guard.get('port_listening') is False
                self.add(report, role+'_temporary_listener_guard', 'pass' if unused else 'inconclusive', guard)
                if not unused:
                    raise StopCollection()
                # Guard proves the name was absent. Mark the attempt before
                # invoking systemd-run so an ambiguous timeout is also cleaned.
                attempted.append((role, unit, address))
                report['temporary_units'].append({'role': role, 'unit': unit, 'address': address, 'port': PORT, 'maximum_runtime_seconds': 180})
                args = ['sudo','-n','systemd-run','--quiet','--collect','--unit='+unit,'--property=User=warehouse','--property=NoNewPrivileges=yes','--property=ProtectSystem=strict','--property=ProtectHome=yes','--property=PrivateTmp=yes','--property=RuntimeMaxSec=180','--property=Restart=no','/usr/bin/python3','-u','-c',listener_code(address)]
                # Shell command must be a single serial line; base64 keeps the
                # listener's internal newlines out of the shell command itself.
                payload = base64.b64encode(listener_code(address).encode()).decode()
                args[-1] = "import base64;exec(base64.b64decode('" + payload + "'))"
                self.command(role, shlex.join(args), timeout=25)
                self.sleep(0.5)
                state = self.guest_json(role, unit_state_script(unit, address))
                own = self.guest_json(role, socket_script(address, address))
                listening = state.get('active_state') == 'active' and state.get('main_pid', 0) > 0 and state.get('ss_exit') == 0 and state.get('address_listening') is True and own.get('source_ip') == address and own.get('connected') is True
                ready_targets[role] = listening
                units[role] = (unit, state.get('main_pid'))
                self.add(report, role+'_temporary_listener_self_connect', 'pass' if listening else 'inconclusive', {'unit': state, 'self_connect': own})
                if not listening:
                    raise StopCollection()
            for source_role, target_role in (('office','iot'), ('iot','office')):
                before = self.session.router()
                probe = self.guest_json(source_role, socket_script(ADDRESSES[source_role], ADDRESSES[target_role]))
                after = self.session.router()
                target_unit, target_pid = units[target_role]
                target_state = self.guest_json(target_role, unit_state_script(target_unit, ADDRESSES[target_role]))
                target_self = self.guest_json(target_role, socket_script(ADDRESSES[target_role], ADDRESSES[target_role]))
                still_listening = ready_targets[target_role] and target_state.get('active_state') == 'active' and target_state.get('main_pid') == target_pid and target_state.get('address_listening') is True and target_self.get('source_ip') == ADDRESSES[target_role] and target_self.get('connected') is True
                deny = deny_counter_evidence(before['rules'], after['rules'], source_role)
                healthy = before['fw4_check_exit'] == 0 and after['fw4_check_exit'] == 0
                status = classify_deny(probe, ADDRESSES[source_role], still_listening, deny) if healthy else 'inconclusive'
                self.add(report, source_role+'_to_'+target_role+'_9000_denied', status, {'probe': probe, 'target_listener_verified': still_listening, 'target_after_probe':target_state,'target_self_after_probe':target_self,'fw4_healthy': healthy, 'deny_counter': deny})
        except StopCollection:
            pass
        except (Exception, KeyboardInterrupt) as error:
            self.add(report, 'isolation_execution', 'inconclusive', {'error_type': type(error).__name__, 'detail': str(error) if isinstance(error, ProtocolError) else 'Inspect the indicated local VM; arbitrary error text is not exported.'})
        finally:
            for role, unit, address in reversed(attempted):
                try:
                    state = self.guest_json(role, unit_state_script(unit, address, stop=True), timeout=30)
                    cleaned = state.get('active_state') == 'inactive' and state.get('ss_exit') == 0 and state.get('port_listening') is False
                    report['cleanup'].append({'role':role,'unit':unit,'verified':cleaned,'state':state})
                    self.add(report, role+'_temporary_listener_removed', 'pass' if cleaned else 'failed', state)
                except (Exception, KeyboardInterrupt) as error:
                    report['cleanup'].append({'role':role,'unit':unit,'verified':False,'error_type':type(error).__name__,'manual_command':'sudo systemctl stop '+unit})
                    self.add(report, role+'_temporary_listener_removed', 'failed', {'error_type':type(error).__name__, 'manual_command':'sudo systemctl stop '+unit})
        return self.finish(report)


def main(argv=None, console_factory=None):
    parser = argparse.ArgumentParser(description='IoT 身份采集与办公/IoT 单端口横向隔离验收')
    parser.add_argument('--phase', choices=('identity','isolation','all'), default='all')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if console_factory is None:
        try:
            from .console import Console
        except ImportError:
            from console import Console
        console_factory = Console
    runner = PathsRunner(console_factory, progress=lambda value: print(value, file=sys.stderr, flush=True))
    try:
        if args.phase == 'identity':
            report = runner.identity()
        elif args.phase == 'isolation':
            report = runner.isolation()
        else:
            identity, isolation = runner.identity(), runner.isolation()
            passed = identity['passed'] and isolation['passed']
            report = {'phase':'all','identity':identity,'isolation':isolation,'passed':passed,'status':'passed' if passed else 'failed' if 'failed' in (identity['status'],isolation['status']) else 'inconclusive'}
    finally:
        runner.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'phase':args.phase,'status':report['status'],'passed':report['passed'],'evidence':str(args.output)},ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1 if report['status']=='failed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
