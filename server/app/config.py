"""Validated local configuration; HTTP routes never mutate these settings."""
import json
import math
from pathlib import Path


def defaults():
    return {'profile':'disabled','retention':{'readings_days':90,'events_days':180},
            'alerts':{'enabled':False,'trigger_seconds':30,'recovery_seconds':30,
                      'temperature_c':{'min':18,'max':28,'hysteresis':1},
                      'humidity_pct':{'min':30,'max':70,'hysteresis':3}}}


def number(value, name, lower, upper):
    if type(value) not in (int,float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError('invalid configuration: '+name)
    return value


def validate(value, demo_mode=False):
    if not isinstance(value,dict) or set(value)-{'profile','retention','alerts'}:
        raise ValueError('invalid configuration keys')
    result=defaults()
    result['profile']=value.get('profile','disabled')
    if result['profile'] not in ('disabled','simulated_demo','custom'):
        raise ValueError('invalid configuration profile')
    retention=value.get('retention',{})
    if not isinstance(retention,dict) or set(retention)-{'readings_days','events_days'}:
        raise ValueError('invalid retention configuration')
    for field in ('readings_days','events_days'):
        days=retention.get(field,result['retention'][field])
        if type(days) is not int or not 1 <= days <= 3650:
            raise ValueError('invalid retention days')
        result['retention'][field]=days
    alerts=value.get('alerts',{})
    if not isinstance(alerts,dict) or set(alerts)-set(result['alerts']):
        raise ValueError('invalid alerts configuration')
    enabled=alerts.get('enabled',False)
    if type(enabled) is not bool:
        raise ValueError('alerts.enabled must be boolean')
    result['alerts']['enabled']=enabled and result['profile']!='disabled' and (demo_mode or result['profile']!='simulated_demo')
    for field in ('trigger_seconds','recovery_seconds'):
        result['alerts'][field]=number(alerts.get(field,30),field,1,86400)
    for field,bounds in (('temperature_c',(-100,200)),('humidity_pct',(0,100))):
        limits=alerts.get(field,result['alerts'][field])
        if not isinstance(limits,dict) or set(limits)!={'min','max','hysteresis'}:
            raise ValueError('threshold requires min, max and hysteresis')
        low=number(limits['min'],field+'.min',*bounds)
        high=number(limits['max'],field+'.max',*bounds)
        hysteresis=number(limits['hysteresis'],field+'.hysteresis',0,high-low if high>low else 0)
        if low>=high or 2*hysteresis>=high-low:
            raise ValueError('threshold range or hysteresis is invalid')
        result['alerts'][field]={'min':low,'max':high,'hysteresis':hysteresis}
    return result


def load(path=None, demo_mode=False):
    if not path:
        return defaults()
    target=Path(path)
    if target.stat().st_size>32768:
        raise ValueError('configuration exceeds 32 KiB')
    return validate(json.loads(target.read_text(encoding='utf-8')),demo_mode)
