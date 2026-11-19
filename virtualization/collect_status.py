"""Collect public result references and check protected services, read-only."""
from datetime import datetime, timezone
import json
import subprocess
import hashlib
from cloud_connection import audit, connect_guangzhou
from runtime import PROJECT, PRIVATE, load_paths
from live_health import observe
from console import Console


def summarize_acceptance(reports):
    """Dates belong to each original run; no history is relabeled as live."""
    values=list(reports.values())
    return {'state':'passed' if values and all(row['passed'] for row in values) else 'incomplete',
        'reports':reports,'is_live_health':False,
        'note':'Historical acceptance for the versions and observation times in the original reports.'}


def combined_state(acceptance, live, protected=True):
    if not protected or live.get('state')=='unhealthy': return 'unhealthy'
    if live.get('state')!='healthy': return 'unknown'
    return 'healthy' if acceptance.get('state')=='passed' else 'incomplete'


def main(argv=None):
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    reports = {}
    for name in ('virtual-matrix','virtual-paths','virtual-remote','virtual-wan-disconnect','virtual-broker-stop','virtual-wan-policy'):
        path = PROJECT/'evidence'/(name+'.json')
        value = json.loads(path.read_text(encoding='utf-8'))
        reports[name] = {'file':'evidence/'+path.name,'passed':value.get('passed') is True,
            'status':value.get('status', 'passed' if value.get('passed') else 'failed'),
            'observed_at':value.get('observed_at'),
            'restoration':value.get('restoration'),'report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'version':value.get('version',value.get('git_commit','unrecorded_legacy_version'))}
    active = subprocess.run([str(load_paths().vmrun),'list'],capture_output=True,text=True,check=True).stdout.splitlines()[1:]
    vms = {role:str(load_paths().vms/role/(role+'.vmx')).casefold() in {s.strip().casefold() for s in active}
           for role in ('router','service','iot','office')}
    host_containers = {}
    for role in ('broker','dashboard'):
        name = 'warehouse-netops-'+role+'-1'
        result = subprocess.run(['docker','inspect','--format','{{json .State.Health.Status}} {{json .HostConfig.PortBindings}}',name],
                                capture_output=True,text=True,check=True)
        status, bindings = result.stdout.strip().split(' ',1)
        host_containers[name] = {'health':json.loads(status),'port_bindings':json.loads(bindings)}
    client = connect_guangzhou()
    try:
        protected = audit(client)==json.loads((PRIVATE/'guangzhou-before.json').read_text(encoding='utf-8'))
    finally:
        client.close()
    acceptance=summarize_acceptance(reports)
    live=observe(Console,connect_guangzhou)
    host_ok = all(row['health']=='healthy' and all(binding['HostIp']=='127.0.0.1'
        for values in row['port_bindings'].values() for binding in values) for row in host_containers.values())
    state=combined_state(acceptance,live,protected)
    passed = state=='healthy' and all(vms.values()) and host_ok
    revision=subprocess.run(['git','rev-parse','HEAD'],cwd=PROJECT,capture_output=True,text=True,check=True).stdout.strip()
    report = {'observed_at':datetime.now(timezone.utc).isoformat(),
        'scope':'One Windows computer, four VMware wired virtual machines and the Guangzhou management endpoint',
        'passed':passed,'historical_acceptance_passed':acceptance['state']=='passed',
        'status':'passed' if passed else state,
        'last_acceptance':acceptance,'live_health':live,'collector_git_commit':revision,
        'reports':reports,'owned_vms_running':vms,'original_host_containers':host_containers,
        'guangzhou_protected_baseline_unchanged':protected,'us_accessed_or_changed':False,
        'not_verified':['Physical Wi-Fi, ESP32 and DHT22','Internet-origin attack testing','Actual Wokwi runtime slider operation'],
        'operator_guide':'docs/demo-runbook.md'}
    target = PROJECT/'evidence/virtual-verification.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':report['status'],'last_acceptance':acceptance['state'],'live_health':live['state'],'guangzhou_protected_baseline_unchanged':protected,'original_host_containers_healthy_and_loopback':host_ok,'evidence':str(target)},ensure_ascii=False))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
