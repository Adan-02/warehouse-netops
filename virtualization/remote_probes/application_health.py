"""Runs on the office VM; read a bounded application-health response."""
import http.client
import json


def application_health(parameters):
    result = {}
    try:
        client = http.client.HTTPConnection(parameters['service_ip'], parameters['service_port'], timeout=4)
        client.request('GET', '/api/health')
        response = client.getresponse()
        result['status'] = response.status
        result['health'] = json.loads(response.read(32768))
        client.close()
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    return result
