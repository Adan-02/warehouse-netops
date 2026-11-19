"""Runs on the office VM; read the public dashboard snapshot without proxies."""
import json
import urllib.request


def office_status(parameters, result):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://' + parameters['service_ip'] + ':8080/api/status', timeout=4) as response:
            value = json.load(response)
            value = {'status': response.status,
                'data': {key: value.get(key) for key in parameters['http_fields']}}
    except Exception as exc:
        value = {'error_type': type(exc).__name__}
    result['http'] = value
