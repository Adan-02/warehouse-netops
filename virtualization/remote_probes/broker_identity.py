"""Runs on the service VM; parses bounded broker connection notices."""
import re
import subprocess

def broker_connections(lines):
    """Mosquitto notice format; unknown formats yield no authentication claim."""
    result = []
    pattern = re.compile(r"New client connected from ([0-9.]+):(\d+) as (warehouse-sim-[A-Za-z0-9_-]{1,80}) \(([^)]*)\)")
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        address, port, client, meta = match.groups()
        username = re.search(r"(?:^|,\s*)u'([^']+)'(?:,|$)", meta)
        if not username or username.group(1) != "cabinet-01" or not 1 <= int(port) <= 65535:
            continue
        result.append({"source_ip": address, "source_port": int(port), "client_id": client, "username": "cabinet-01", "notice": "New client connected"})
    return result[-12:]

def probe(parameters):
    p=subprocess.run(['sudo','-n','docker','compose','--project-directory',parameters['server_directory'],'--project-name','warehouse-netops','logs','--no-color','--timestamps','--since','1h','--tail','2000','broker'],capture_output=True,text=True,timeout=20)
    r={'command_exit':p.returncode,'connections':broker_connections(p.stdout.splitlines()),'window':'last 1h, at most 2000 lines','unrecognized_notice_count':sum('New client connected from ' in line for line in p.stdout.splitlines())}
    return r
