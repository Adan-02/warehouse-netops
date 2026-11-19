"""Runs on the IoT VM; source-bound publication identity without secrets."""
import json
import math
import subprocess
import time

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

def probe(parameters):
    r={}
    p=subprocess.run(['systemctl','show',parameters['publisher_unit'],'--property=MainPID,ActiveState'],capture_output=True,text=True)
    state=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
    pid=int(state.get('MainPID','0'));r['main_pid']=pid;r['unit_active']=state.get('ActiveState')=='active'
    try:
     args=open('/proc/'+str(pid)+'/cmdline','rb').read().decode().split('\0')
     r['source_argument_matches']='--source-ip' in args and args[args.index('--source-ip')+1]==parameters['iot_ip']
     r['publisher_script_matches']='/opt/warehouse-netops/tools/publish_sensor.py' in args
    except (OSError,IndexError):
     r['source_argument_matches']=False;r['publisher_script_matches']=False
    p=subprocess.run(['sudo','-n','journalctl','-u',parameters['publisher_unit'],'--since=-120s','-n','100','--no-pager','-o','json'],capture_output=True,text=True)
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
     if not local.startswith(parameters['iot_ip']+':') or peer!=parameters['service_ip']+':1883': continue
     try: port=int(local.rsplit(':',1)[1])
     except ValueError: continue
     r['mqtt_sockets'].append({'source_ip':parameters['iot_ip'],'source_port':port,'peer_ip':parameters['service_ip'],'peer_port':1883,'main_pid_matches':True})
    return r
