"""Read-only fresh business observations, separate from historical acceptance."""
from datetime import datetime, timezone

try:
    from .lab_session import LabSession
    from .probes import online,encoded_python,extract_json,remote_sources,probe_parameters,probe_output
    from .lab_topology import CLOUD_IP,SERVICE_IP,SERVICE_PORT
except ImportError:
    from lab_session import LabSession
    from probes import online,encoded_python,extract_json,remote_sources,probe_parameters,probe_output
    from lab_topology import CLOUD_IP,SERVICE_IP,SERVICE_PORT


def observe(console_factory, cloud_factory=None):
    report={'observed_at':datetime.now(timezone.utc).isoformat(),'state':'unknown','components':{}}
    runner=LabSession(console_factory,cloud_factory=cloud_factory)
    try:
        local=runner.local_probe()
        report['components']['local_business']={'state':'healthy' if online(local) else 'unhealthy','source':'office','evidence':local}
        script=(remote_sources('application_health')+'\n'
            +probe_parameters({'service_ip':SERVICE_IP,'service_port':SERVICE_PORT})
            +'result=application_health(parameters)\n'+probe_output('WH_LIVE_HEALTH_'))
        result=runner.console('office').command(encoded_python(script,role='office'),timeout=15)
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
