"""Read-only fresh business observations, separate from historical acceptance."""
from datetime import datetime, timezone
import json
import time

try:
    from .check_faults import FaultRunner, online, CLOUD_IP
    from .check_matrix import encoded_python, extract_json
except ImportError:
    from check_faults import FaultRunner, online, CLOUD_IP
    from check_matrix import encoded_python, extract_json


def observe(console_factory, cloud_factory=None):
    report={'observed_at':datetime.now(timezone.utc).isoformat(),'state':'unknown','components':{}}
    runner=FaultRunner(console_factory,cloud_factory=cloud_factory)
    try:
        local=runner.local_probe()
        report['components']['local_business']={'state':'healthy' if online(local) else 'unhealthy','source':'office','evidence':local}
        script="""import http.client,json,socket
r={}
try:
 c=http.client.HTTPConnection('192.168.30.10',8080,timeout=4)
 c.request('GET','/api/health');v=c.getresponse();r['status']=v.status;r['health']=json.loads(v.read(32768));c.close()
except Exception as e:r['error_type']=type(e).__name__
print('\\nWH_LIVE_HEALTH_'+json.dumps(r))
"""
        result=runner.matrix.console('office').command(encoded_python(script),timeout=15)
        data=extract_json(result['output'],'WH_LIVE_HEALTH_')
        report['components']['application_health']={'state':data.get('health',{}).get('state','unknown'),'source':'office','evidence':data}
        if cloud_factory:
            try:
                remote=runner.cloud_probe()
                report['components']['guangzhou_business']={'state':'healthy' if online(remote,CLOUD_IP) else 'unhealthy','source':'guangzhou','evidence':remote}
            except Exception as error:
                report['components']['guangzhou_business']={'state':'unknown','source':'guangzhou','error_type':type(error).__name__}
        states=[v['state'] for v in report['components'].values()]
        report['state']='unhealthy' if 'unhealthy' in states else 'unknown' if 'unknown' in states else 'healthy'
    except Exception as error:
        report['error_type']=type(error).__name__
    finally: runner.close()
    return report
