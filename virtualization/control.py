"""Start/status/gracefully stop only the four owned warehouse virtual machines."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from console import Console
from runtime import VMRUN, VMS, vmrun

ROLES = ('router','service','iot','office')


def running():
    output = subprocess.run([str(VMRUN),'list'],capture_output=True,text=True,check=True).stdout
    return {str(Path(line.strip()).resolve()).casefold() for line in output.splitlines()[1:] if line.strip()}


def is_running(role, active):
    return str((VMS/role/(role+'.vmx')).resolve()).casefold() in active


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','start','stop'))
    parser.add_argument('--wait', action='store_true', help='Wait for fresh local and Guangzhou business health after start')
    parser.add_argument('--timeout',type=int,default=300)
    args = parser.parse_args()
    if args.action == 'start':
        for role in ROLES:
            if not is_running(role,running()):
                vmrun('start',role,'nogui')
                print(role+': starting',flush=True)
    elif args.action == 'stop':
        for role in reversed(ROLES):
            if not is_running(role,running()):
                continue
            console = Console(role)
            try:
                command = '(sleep 1; poweroff) >/dev/null 2>&1 &' if role=='router' else 'sudo -n systemctl poweroff --no-block'
                console.command(command,timeout=10)
            finally:
                console.close()
            deadline = time.monotonic()+120
            while is_running(role,running()):
                if time.monotonic()>=deadline:
                    raise RuntimeError(role+': graceful shutdown timed out; no forced power-off was attempted')
                time.sleep(2)
            print(role+': stopped',flush=True)
    active = running()
    print(json.dumps({role:'running' if is_running(role,active) else 'stopped' for role in ROLES},indent=2))
    if args.wait:
        if args.action != 'start': parser.error('--wait requires start')
        from live_health import observe
        from cloud_connection import connect_guangzhou
        deadline=time.monotonic()+args.timeout
        while True:
            health=observe(Console,connect_guangzhou)
            print(json.dumps({'business_readiness':health['state'],'observed_at':health['observed_at']}),flush=True)
            if health['state']=='healthy': break
            if time.monotonic()>=deadline: raise RuntimeError('VM processes are running but business readiness did not pass')
            time.sleep(5)


if __name__ == '__main__':
    main()
