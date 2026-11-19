"""Read a bounded public HTTP snapshot through a freshly source-bound socket."""
import http.client
import json
import socket
import subprocess
import time


def http_status(parameters):
    result = {'source_ip': None, 'connected': False, 'status': None}
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(4)
    try:
        connection.bind((parameters['source_ip'], 0))
        result['source_ip'] = connection.getsockname()[0]
        connection.connect((parameters['service_ip'], parameters['service_port']))
        result['connected'] = True
        client = http.client.HTTPConnection(parameters['service_ip'], parameters['service_port'], timeout=4)
        client.sock = connection
        client.request('GET', '/api/status', headers={'Connection': 'close'})
        response = client.getresponse()
        result['status'] = response.status
        value = json.loads(response.read(16384))
        result['data'] = {key: value.get(key) for key in parameters['public_fields']}
        client.close()
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    finally:
        connection.close()
    if parameters['handshake']:
        try:
            completed = subprocess.run(['sudo', '-n', '/usr/bin/wg', 'show', 'wg-warehouse',
                'latest-handshakes'], capture_output=True, text=True, timeout=4)
            values = [int(line.split()[-1]) for line in completed.stdout.splitlines() if len(line.split()) == 2]
            result['wireguard'] = {'command_exit': completed.returncode,
                'latest_handshake': max(values, default=0), 'observed_at': time.time()}
        except Exception as exc:
            result['wireguard'] = {'error_type': type(exc).__name__}
    return result
