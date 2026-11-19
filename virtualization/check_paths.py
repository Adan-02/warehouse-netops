"""Identity-path and Office/IoT isolation checks on the real wired VM lab.

Identity collection is read-only. Isolation creates only two private, uniquely
named, short-lived TCP-9000 listeners and always stops them in finally. Nothing
changes firewall policy, long-running services, cloud servers, or credentials.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import inspect
import json
import math
from pathlib import Path
import re
import shlex
import sys
import time
import uuid
import zlib
import base64

try:
    from .check_matrix import MatrixRunner, ProtocolError, ADDRESSES, deny_counter_evidence, extract_json, network_ready
except ImportError:
    from check_matrix import MatrixRunner, ProtocolError, ADDRESSES, deny_counter_evidence, extract_json, network_ready


PORT = 9000


class StopCollection(Exception):
    """A failed prerequisite already has a structured report entry."""


def public_payload(value):
    if not isinstance(value, dict) or value.get("device_id") != "cabinet-01":
        return None
    seq, temp, humidity = (value.get(key) for key in ("seq", "temperature_c", "humidity_pct"))
    if type(seq) is not int or not 1 <= seq <= 2**53 - 1:
        return None
    if any(type(item) not in (int, float) or not math.isfinite(item) for item in (temp, humidity)) or not 0 <= humidity <= 100:
        return None
    return {"device_id": "cabinet-01", "seq": seq, "temperature_c": temp, "humidity_pct": humidity}


def puback_records(records, pid, now):
    """Keep only current-process, recent, successful, schema-valid JSON samples."""
    result = []
    marker = "SIMULATED PUBACK accepted "
    for record in records:
        if not isinstance(record, dict) or str(record.get("_PID")) != str(pid) or not pid:
            continue
        message = record.get("MESSAGE")
        if not isinstance(message, str) or not message.startswith(marker):
            continue
        try:
            stamp = int(record["__REALTIME_TIMESTAMP"]) / 1_000_000
            value = public_payload(json.loads(message[len(marker):]))
        except (KeyError, ValueError, TypeError):
            continue
        if value is not None and 0 <= now - stamp <= 20:
            result.append({**value, "accepted_epoch": stamp, "age_seconds": round(now - stamp, 3)})
    return result[-12:]


def broker_connections(lines):
    """Mosquitto notice format; unknown formats yield no authentication claim."""
    result = []
    pattern = re.compile(r"New client connected from ([0-9.]+):(\d+) as (warehouse-sim-[A-Za-z0-9_-]{1,80}) \(([^)]*)\)")
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        address, port, client, meta = match.groups()
        username = re.search(r"(?:^|,\s*)u'([^']+)'(?:,|$)", meta)
        if not username or username.group(1) != "cabinet-01" or not 1 <= int(port) <= 65535:
            continue
        result.append({"source_ip": address, "source_port": int(port), "client_id": client, "username": "cabinet-01", "notice": "New client connected"})
    return result[-12:]


def compressed_python(script):
    payload = base64.b64encode(zlib.compress(script.encode("utf-8"), level=9)).decode("ascii")
    command = 'python3 -c "import base64,zlib;exec(zlib.decompress(base64.b64decode(\'' + payload + '\')))"'
    if len(command.encode("utf-8")) > 2900:
        raise ProtocolError("Path-check command exceeds the conservative serial line limit.")
    return command


def iot_script():
    return "import json,math,subprocess,time\n" + inspect.getsource(public_payload) + "\n" + inspect.getsource(puback_records) + "\n" + """r={}
p=subprocess.run(['systemctl','show','warehouse-sensor-sim','--property=MainPID,ActiveState'],capture_output=True,text=True)
state=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
pid=int(state.get('MainPID','0'));r['main_pid']=pid;r['unit_active']=state.get('ActiveState')=='active'
try:
 args=open('/proc/'+str(pid)+'/cmdline','rb').read().decode().split('\\0')
 r['source_argument_matches']='--source-ip' in args and args[args.index('--source-ip')+1]=='192.168.20.100'
 r['publisher_script_matches']='/opt/warehouse-netops/tools/publish_sensor.py' in args
except (OSError,IndexError):
 r['source_argument_matches']=False;r['publisher_script_matches']=False
p=subprocess.run(['sudo','-n','journalctl','-u','warehouse-sensor-sim','--since=-120s','-n','100','--no-pager','-o','json'],capture_output=True,text=True)
records=[]
for line in p.stdout.splitlines():
 try: records.append(json.loads(line))
 except ValueError: pass
r['journal_exit']=p.returncode;r['pubacks']=puback_records(records,pid,time.time())
p=subprocess.run(['ss','-H','-tnp'],capture_output=True,text=True);r['mqtt_sockets']=[]
for line in p.stdout.splitlines():
 fields=line.split()
 if len(fields)<5 or fields[0]!='ESTAB' or ('pid='+str(pid)+',') not in line: continue
 local,peer=fields[3:5]
 if not local.startswith('192.168.20.100:') or peer!='192.168.30.10:1883': continue
 try: port=int(local.rsplit(':',1)[1])
 except ValueError: continue
 r['mqtt_sockets'].append({'source_ip':'192.168.20.100','source_port':port,'peer_ip':'192.168.30.10','peer_port':1883,'main_pid_matches':True})
"""


def broker_script():
    return "import re,subprocess\n" + inspect.getsource(broker_connections) + "\n" + """p=subprocess.run(['sudo','-n','docker','compose','--project-directory','/opt/warehouse-netops/server','--project-name','warehouse-netops','logs','--no-color','--timestamps','--since','1h','--tail','2000','broker'],capture_output=True,text=True,timeout=20)
r={'command_exit':p.returncode,'connections':broker_connections(p.stdout.splitlines()),'window':'last 1h, at most 2000 lines','unrecognized_notice_count':sum('New client connected from ' in line for line in p.stdout.splitlines())}
"""


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
        self.matrix = MatrixRunner(console_factory)
        self.sleep = sleep
        self.progress = progress or (lambda message: None)

    def close(self):
        self.matrix.close()

    def command(self, role, command, timeout=30):
        result = self.matrix.console(role).command(command, timeout=timeout)
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
            inventory = self.matrix.guest('iot', 'inventory')
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
                inventory = self.matrix.guest(role, 'inventory')
                ready = network_ready(role, inventory)
                self.add(report, role+'_single_nic_and_gateway', 'pass' if ready else 'inconclusive', inventory)
                if not ready:
                    raise StopCollection()
            initial = self.matrix.router()
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
                before = self.matrix.router()
                probe = self.guest_json(source_role, socket_script(ADDRESSES[source_role], ADDRESSES[target_role]))
                after = self.matrix.router()
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
