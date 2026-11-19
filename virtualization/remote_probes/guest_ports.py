"""Runs on a Debian VM; probe selected service ports from its assigned IP."""
import socket
import time


def guest_ports(parameters, result):
    ports = result['ports'] = {}
    for port in parameters['ports']:
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(2)
        row = {'connected': False, 'source_ip': None, 'errno': None}
        start = time.monotonic()
        try:
            connection.bind((parameters['source_ip'], 0))
            row['source_ip'] = connection.getsockname()[0]
            connection.connect((parameters['service_ip'], port))
            row['connected'] = True
        except OSError as exc:
            row['errno'] = exc.errno
            row['error_type'] = type(exc).__name__
        finally:
            connection.close()
        row['elapsed_ms'] = round((time.monotonic() - start) * 1000, 1)
        ports[str(port)] = row
