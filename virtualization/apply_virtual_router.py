"""Transactionally apply the reviewed local virtual-router configuration."""
import argparse
import json
import re
from datetime import datetime, timezone
from console import Console
from console_files import upload
from runtime import PROJECT, PRIVATE, MACS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wireguard', action='store_true')
    args = parser.parse_args()
    console = Console('router')
    try:
        upload(console, '/root/warehouse-virtual-router.sh', (PROJECT/'router/virtual-router.sh').read_bytes(), 0o700)
        command = 'sh /root/warehouse-virtual-router.sh prepare'
        for option, mac in zip(('wan', 'office', 'iot', 'service'), MACS['router']):
            command += f' --{option}-mac {mac}'
        command += f" --office-client-mac {MACS['office'][0]} --iot-client-mac {MACS['iot'][0]}"
        if args.wireguard:
            upload(console, '/root/warehouse-wg-private', (PRIVATE/'router-wg.key').read_bytes())
            upload(console, '/root/warehouse-wg-peer', (PRIVATE/'guangzhou-wg.pub').read_bytes())
            command += ' --wg-endpoint 123.207.240.142 --wg-private-key-file /root/warehouse-wg-private --wg-peer-public-key-file /root/warehouse-wg-peer'
        upload(console, '/root/warehouse-prepare.sh', ('#!/bin/sh\nset -eu\n'+command+'\n').encode(), 0o700)
        prepared = console.command('sh /root/warehouse-prepare.sh', 45)
        matches = re.findall(r'Prepared (/root/warehouse-virtual-stages/[a-zA-Z0-9-]+)', prepared['output'])
        if prepared['exit_code'] or len(matches) != 1:
            raise RuntimeError('Router prepare failed; inspect local console, no apply attempted')
        stage = matches[0]
        (PRIVATE/'router-stage.txt').write_text(stage, encoding='utf-8')
        applied = console.command('sh /root/warehouse-virtual-router.sh apply '+stage, 90)
        verify = console.command('fw4 check && ip -4 addr show && ip -4 route', 35)
        passed = applied['exit_code'] == verify['exit_code'] == 0 and all(ip in verify['output'] for ip in ('192.168.10.1','192.168.20.1','192.168.30.1'))
        report = {'observed_at': datetime.now(timezone.utc).isoformat(), 'wireguard_requested': args.wireguard, 'stage': stage, 'passed': passed, 'apply': applied, 'verification': verify}
        (PROJECT/'evidence/virtual-router-apply.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({'passed':passed,'stage':stage,'wireguard_requested':args.wireguard}))
        return 0 if passed else 1
    finally:
        console.close()


if __name__ == '__main__':
    raise SystemExit(main())
