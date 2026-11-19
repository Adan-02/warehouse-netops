"""Four bounded MQTT authorization checks from the real IoT source address."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import time
try:
    from .lab_session import LabSession
    from .probes import encoded_python,extract_json,network_ready
except ImportError:
    from lab_session import LabSession
    from probes import encoded_python,extract_json,network_ready


def script(mode,prefix):
    if mode not in ('anonymous','wrong_password','other_topic','device_read'): raise ValueError(mode)
    return f"""import json,time,threading,socket
from pathlib import Path
import paho.mqtt.client as m
d=dict(l.split('=',1) for l in Path('/etc/warehouse/device.env').read_text().splitlines() if l and not l.startswith('#') and '=' in l)
mode={mode!r};r={{'source_ip':'192.168.20.100','mode':mode}};done=threading.Event();received=[];acks=[]
c=m.Client(m.CallbackAPIVersion.VERSION2,client_id='warehouse-policy-'+mode,protocol=m.MQTTv5)
if mode!='anonymous':c.username_pw_set('cabinet-01','deliberately-invalid-policy-password' if mode=='wrong_password' else d['MQTT_DEVICE_PASSWORD'])
def connected(c,u,f,rc,p):r['connack']=rc.value;done.set()
def published(c,u,mid,rc,p):acks.append(rc.value)
def subscribed(c,u,mid,rc,p):r['suback']=[v.value for v in rc]
c.on_connect=connected;c.on_publish=published;c.on_subscribe=subscribed;c.on_message=lambda c,u,msg:received.append(msg.topic)
try:
 c.connect_timeout=4;c.connect('192.168.30.10',1883,keepalive=15,bind_address='192.168.20.100');c.loop_start();done.wait(5)
 if r.get('connack')==0 and mode=='other_topic':
  info=c.publish('warehouse/other/telemetry','policy-probe',qos=1);info.wait_for_publish(4);time.sleep(.2);r['puback']=acks[-1] if acks else None
 if r.get('connack')==0 and mode=='device_read':
  c.subscribe('warehouse/cabinet-01/telemetry',qos=1);time.sleep(.5)
  info=c.publish('warehouse/cabinet-01/telemetry','{{\"policy_probe\":true}}',qos=1);info.wait_for_publish(4);time.sleep(6);r['probe_puback']=acks[-1] if acks else None;r['delivered']=len(received)
except Exception as e:r['error_type']=type(e).__name__
finally:c.disconnect();c.loop_stop()
print('\\n'+{prefix!r}+json.dumps(r))
"""


def classify(row):
    if row.get('source_ip')!='192.168.20.100' or 'error_type' in row:return False
    if row['mode'] in ('anonymous','wrong_password'):return row.get('connack') in (0x86,0x87)
    if row['mode']=='other_topic':return row.get('connack')==0 and row.get('puback')==0x87
    return row.get('connack')==0 and isinstance(row.get('suback'),list) and row.get('probe_puback')==0 and row.get('delivered')==0


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'evidence/virtual-mqtt-policy.json')
    args=parser.parse_args()
    try:
        from .console import Console
    except ImportError:
        from console import Console
    runner=LabSession(Console)
    report={'observed_at':datetime.now(timezone.utc).isoformat(),'scope':'MQTT negative authorization from IoT VM, no routing mutation','checks':[]}
    def add(name,passed,evidence):report['checks'].append({'name':name,'status':'pass' if passed else 'failed','evidence':evidence})
    try:
        inventory=runner.guest('iot','inventory');ready=network_ready('iot',inventory)
        add('iot_single_nic_and_gateway',ready,inventory)
        if not ready:raise RuntimeError('IoT prerequisite unavailable')
        before=runner.guest('office','probes')['http']['data']
        add('baseline_sampling_online',before.get('data_status')=='online' and before.get('broker_connected') is True,before)
        for mode in ('anonymous','wrong_password','other_topic','device_read'):
            prefix='WH_MQTT_'+mode+'_'
            command=encoded_python(script(mode,prefix))
            result=runner.console('iot').command(command,timeout=22)
            value=extract_json(result['output'],prefix)
            add(mode+'_denied',result['exit_code']==0 and classify(value),value)
        after=runner.guest('office','probes')['http']['data']
        add('normal_sampling_survived',after.get('data_status')=='online' and after.get('broker_connected') is True and after.get('seq',0)>before.get('seq',0),after)
    except Exception as error:add('collection_completed',False,{'error_type':type(error).__name__})
    finally:runner.close()
    report['passed']=all(row['status']=='pass' for row in report['checks']);report['status']='passed' if report['passed'] else 'failed'
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'checks':[{k:r[k] for k in ('name','status')} for r in report['checks']]}))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
