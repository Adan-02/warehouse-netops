"""Update only the owned IoT simulator through COM1, preserving credentials."""
import base64
from datetime import datetime,timezone
import hashlib
import json
import shlex
from console import Console
from runtime import PROJECT

TARGET='/opt/warehouse-netops/tools/publish_sensor.py'


def main():
    data=(PROJECT/'tools/publish_sensor.py').read_bytes()
    digest=hashlib.sha256(data).hexdigest()
    temporary='/home/warehouse/warehouse-publisher-'+digest[:12]+'.new'
    previous=TARGET+'.before-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    console=Console('iot')
    attempted=False
    def checked(command,timeout=30):
        result=console.command(command,timeout)
        if result['exit_code']:raise RuntimeError('IoT publisher update command failed')
        return result['output']
    try:
        checked('test "$(hostname)" = warehouse-iot && test -f /etc/warehouse/device.env && test -f '+TARGET)
        encoded=base64.b64encode(data).decode()
        for offset in range(0,len(encoded),1400):
            checked('printf %s '+shlex.quote(encoded[offset:offset+1400])+(' > ' if offset==0 else ' >> ')+temporary+'.b64')
        checked('base64 -d '+temporary+'.b64 > '+temporary+' && python3 -m py_compile '+temporary+' && rm '+temporary+'.b64')
        if digest not in checked('sha256sum '+temporary):raise RuntimeError('IoT source checksum mismatch')
        checked('sudo -n cp -p '+TARGET+' '+previous)
        attempted=True
        checked('sudo -n systemctl stop warehouse-sensor-sim && sudo -n install -m 0644 '+temporary+' '+TARGET+' && sudo -n systemctl start warehouse-sensor-sim',45)
        checked('systemctl is-active --quiet warehouse-sensor-sim')
        proof={'observed_at':datetime.now(timezone.utc).isoformat(),'updated':True,'sha256':digest,
            'previous':previous,'credentials_changed':False,'scope':'IoT simulator only; no network or cloud mutation'}
        (PROJECT/'evidence/publisher-update.local.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
        print(json.dumps({'updated':True,'sha256':digest}))
    except BaseException:
        if attempted:
            checked('sudo -n cp -p '+previous+' '+TARGET+' && sudo -n systemctl restart warehouse-sensor-sim',40)
        raise
    finally:console.close()


if __name__=='__main__':main()
