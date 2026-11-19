#!/usr/bin/python3 -I
"""Zero-argument root helper for one project's public WireGuard status."""
import json
import os
import subprocess
import sys


def main():
    if len(sys.argv)!=1 or os.geteuid()!=0:
        raise SystemExit(64)
    os.environ['PATH']='/usr/sbin:/usr/bin:/sbin:/bin'
    def read(*args):
        result=subprocess.run(args,capture_output=True,text=True,timeout=3,check=True)
        if len(result.stdout)>16384:raise ValueError('Project status exceeds limit')
        return result.stdout.strip()
    peers=read('wg','show','wg-warehouse','peers').splitlines()
    handshakes={line.split()[0]:int(line.split()[1]) for line in read('wg','show','wg-warehouse','latest-handshakes').splitlines() if len(line.split())==2}
    routes=json.loads(read('ip','-j','-4','route','get','192.168.30.10','from','10.89.0.1'))
    device=routes[0].get('dev') if len(routes)==1 else None
    print(json.dumps({'peers':peers,'handshakes':handshakes,'service_route_device':device}))


if __name__=='__main__':
    main()
