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


def start_guest(role):
    try:
        vmrun('start',role,'nogui')
    except subprocess.CalledProcessError as error:
        detail=str(error.stdout or error.stderr or '').strip()
        if '文件正在使用中' in detail or 'FILE_ALREADY_LOCKED' in detail or 'file is already in use' in detail.casefold():
            raise RuntimeError(role+': VMware configuration is in use; close its powered-off VMware tab and retry. Do not remove lock files.') from None
        raise RuntimeError(role+': VMware start failed (exit '+str(error.returncode)+'): '+detail[:500]) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','start','stop'))
    parser.add_argument('--wait', action='store_true', help='Wait for fresh local and Guangzhou business health after start')
    parser.add_argument('--timeout',type=int,default=600,
                        help='Business-readiness budget in seconds (default: 600)')
    args = parser.parse_args()
    if args.timeout <= 0: parser.error('--timeout must be a positive number of seconds')
    if args.wait and args.action != 'start': parser.error('--wait requires start')
    if args.action == 'start':
        for role in ROLES:
            if not is_running(role,running()):
                start_guest(role)
                print(role+': starting',flush=True)
    elif args.action == 'stop':
        for role in reversed(ROLES):
            if not is_running(role,running()):
                continue
            console = Console(role)
            try:
                # Console appends "; printf ..." to collect the exit code, so
                # leave a foreground command after the background separator.
                command = '(sleep 2; poweroff) >/dev/null 2>&1 & true' if role=='router' else 'sudo -n systemctl poweroff --no-block'
                result = console.command(command,timeout=10)
                if result['exit_code'] != 0:
                    raise RuntimeError(role+': graceful shutdown command failed (exit code '+str(result['exit_code'])+'); no forced power-off was attempted')
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
