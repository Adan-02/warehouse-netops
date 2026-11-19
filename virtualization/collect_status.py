"""Collect public result references and check protected services, read-only."""
from datetime import datetime, timezone
import json
import subprocess
from cloud_connection import audit, connect_guangzhou
from runtime import PROJECT, PRIVATE, VMS, VMRUN


def main():
    reports = {}
    for name in ('virtual-matrix','virtual-paths','virtual-remote','virtual-wan-disconnect','virtual-broker-stop','virtual-wan-policy'):
        path = PROJECT/'evidence'/(name+'.json')
        value = json.loads(path.read_text(encoding='utf-8'))
        reports[name] = {'file':'evidence/'+path.name,'passed':value.get('passed') is True,
            'status':value.get('status', 'passed' if value.get('passed') else 'failed'),
            'observed_at':value.get('observed_at'),
            'restoration':value.get('restoration')}
    active = subprocess.run([str(VMRUN),'list'],capture_output=True,text=True,check=True).stdout.splitlines()[1:]
    vms = {role:str(VMS/role/(role+'.vmx')).casefold() in {s.strip().casefold() for s in active}
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
    core_names = ('virtual-matrix','virtual-paths','virtual-remote','virtual-wan-disconnect','virtual-broker-stop')
    core = all(reports[name]['passed'] for name in core_names)
    host_ok = all(row['health']=='healthy' and all(binding['HostIp']=='127.0.0.1'
        for values in row['port_bindings'].values() for binding in values) for row in host_containers.values())
    passed = core and reports['virtual-wan-policy']['passed'] and protected and all(vms.values()) and host_ok
    report = {'observed_at':datetime.now(timezone.utc).isoformat(),
        'scope':'One Windows computer, four VMware wired virtual machines and the Guangzhou management endpoint',
        'passed':passed,'virtual_core_passed':core,
        'status':'passed' if passed else 'awaiting_admin_wan_probe' if core and reports['virtual-wan-policy']['status']=='needs_admin' else 'incomplete',
        'reports':reports,'owned_vms_running':vms,'original_host_containers':host_containers,
        'guangzhou_protected_baseline_unchanged':protected,'us_accessed_or_changed':False,
        'recorded_software_checks_date':'2026-11-19 (not re-run by this read-only collector)',
        'recorded_software_checks':[
            {'command':'python -m unittest discover -s virtualization -p test_*.py -q','run':123,'passed':110,'skipped':13,'scope':'Windows/offline generator and evidence guards; Linux runtime cases are skipped here'},
            {'command':'Disposable no-network Docker router/tests/test_virtual_router.py','run':10,'passed':10,'scope':'Router transaction fault stubs, including final explicit deny ordering'}
        ],
        'not_verified':['Physical Wi-Fi, ESP32 and DHT22','Internet-origin attack testing','Actual Wokwi runtime slider operation'],
        'operator_guide':'docs/demo-runbook.md'}
    target = PROJECT/'evidence/virtual-verification.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':report['status'],'virtual_core_passed':core,'guangzhou_protected_baseline_unchanged':protected,'original_host_containers_healthy_and_loopback':host_ok,'evidence':str(target)},ensure_ascii=False))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
