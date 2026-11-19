"""Runs on the service VM; confirm listeners and source-bound self-connects."""
import socket
import subprocess


def service_listeners(parameters, result):
    ports = parameters['ports']
    result['listeners'] = [line for line in subprocess.check_output(['ss', '-H', '-ltn'], text=True).splitlines()
        if any(line.split()[3].endswith(':' + str(port)) for port in ports)]
    connected = result['self_connect'] = {}
    for port in ports:
        try:
            with socket.create_connection((parameters['service_ip'], port), timeout=2,
                    source_address=(parameters['service_ip'], 0)):
                connected[str(port)] = True
        except OSError:
            connected[str(port)] = False
