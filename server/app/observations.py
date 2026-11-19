"""Consume small, receiver-stamped, read-only network observation snapshots."""
from datetime import datetime,timezone
import json
from pathlib import Path
import stat
import time


def epoch(value):
    if not isinstance(value,str) or len(value)>64:
        raise ValueError('invalid observation time')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('observation time needs timezone')
    return parsed.timestamp()


class ObservationReader:
    def __init__(self,directory=None,clock=time.time,ttl=35):
        self.directory=Path(directory) if directory else None
        self.clock=clock
        self.ttl=ttl
        self.started_at=clock()
        self.initial={}
        if self.directory:
            for name in ('local.json','remote.json'):
                try:
                    value=(self.directory/name).lstat()
                    self.initial[name]=(value.st_mtime_ns,value.st_size,value.st_ino)
                except OSError:
                    pass

    @staticmethod
    def unknown(source,reason):
        return {'state':'unknown','source':source,'observed_at':None,'evidence':{'reason':reason}}

    def read(self):
        result={key:self.unknown(source,'observation_unavailable') for key,source in
                (('wan','site'),('wireguard','site'),('remote_http','guangzhou'),('remote_wireguard','guangzhou'))}
        if not self.directory:
            return result
        for filename,observer,allowed in (('local.json','site',('wan','wireguard')),
                                         ('remote.json','guangzhou',('remote_http','wireguard'))):
            try:
                target=self.directory/filename
                info=target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size>32768:
                    raise ValueError('unsafe_snapshot_file')
                if self.initial.get(filename)==(info.st_mtime_ns,info.st_size,info.st_ino):
                    raise ValueError('awaiting_post_start_observation')
                now=self.clock()
                if not 0 <= now-info.st_mtime <= self.ttl:
                    raise ValueError('snapshot_expired')
                with target.open('r',encoding='utf-8') as stream:
                    payload=stream.read(32769)
                if len(payload.encode('utf-8'))>32768:
                    raise ValueError('snapshot_too_large')
                value=json.loads(payload)
                if value.get('schema_version')!=1 or value.get('observer_id')!=observer:
                    raise ValueError('unexpected_observer')
                received=epoch(value.get('received_at'))
                if not 0 <= now-received <= self.ttl:
                    raise ValueError('snapshot_expired')
                components=value.get('components')
                if not isinstance(components,dict) or set(components)-set(allowed):
                    raise ValueError('invalid_components')
                for key in allowed:
                    destination='remote_wireguard' if observer=='guangzhou' and key=='wireguard' else key
                    row=components.get(key)
                    if not isinstance(row,dict) or row.get('state') not in ('healthy','unhealthy','unknown'):
                        result[destination]=self.unknown(observer,'component_unavailable')
                        continue
                    observed=epoch(row.get('observed_at'))
                    if observed>received+5 or now-observed>self.ttl or not isinstance(row.get('evidence',{}),dict):
                        result[destination]=self.unknown(observer,'component_expired_or_invalid')
                        continue
                    result[destination]={'state':row['state'],'source':observer,
                        'observed_at':row['observed_at'],'received_at':value['received_at'],
                        'evidence':row.get('evidence',{})}
            except (OSError,ValueError,TypeError,AttributeError) as error:
                reason=str(error) if isinstance(error,ValueError) else 'invalid_or_unavailable_snapshot'
                for key in allowed:
                    destination='remote_wireguard' if observer=='guangzhou' and key=='wireguard' else key
                    result[destination]=self.unknown(observer,reason)
        return result
