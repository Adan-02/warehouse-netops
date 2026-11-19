"""Bounded read queries; exports use independent read-only SQLite snapshots."""
from contextlib import closing
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3


def iso_timestamp(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value is not None else None


def range_bounds(start,end):
    values=[]
    for item in (start,end):
        if not isinstance(item,str) or len(item)>64:
            raise ValueError('start and end must be timezone-aware ISO 8601 timestamps')
        try:
            parsed=datetime.fromisoformat(item.replace('Z','+00:00'))
            if parsed.tzinfo is None: raise ValueError()
            stamp=parsed.timestamp()
        except (ValueError,OverflowError):
            raise ValueError('start and end must be timezone-aware ISO 8601 timestamps') from None
        values.append(stamp)
    if not 0<values[1]-values[0]<=7*86400:
        raise ValueError('time range must be positive and at most 7 days')
    return values


def recent_readings(connection,limit=120,start=None,end=None):
    if (start is None) != (end is None):
        raise ValueError('start and end must be supplied together')
    parameters=[]
    where=''
    if start is not None:
        first,last=range_bounds(start,end)
        where=' WHERE received_at>=? AND received_at<?'
        parameters.extend((first,last))
    rows=connection.execute('SELECT seq,temperature_c,humidity_pct,received_at FROM readings'+where+' ORDER BY id DESC LIMIT ?',(*parameters,limit)).fetchall()
    return [{'seq':seq,'temperature_c':temperature,'humidity_pct':humidity,'received_at':iso_timestamp(received)}
            for seq,temperature,humidity,received in reversed(rows)]


def open_reader(path):
    # Separate read-only connections keep exports outside the ingestion lock.
    target=Path(path).resolve().as_uri()+'?mode=ro'
    connection=sqlite3.connect(target,uri=True,timeout=2)
    connection.execute('PRAGMA query_only=ON')
    return connection


def aggregate_history(path,device_id,start,end,resolution='auto'):
    start_epoch,end_epoch=range_bounds(start,end)
    if resolution not in ('auto','60','300'):
        raise ValueError('resolution must be auto, 60 or 300')
    step=(60 if end_epoch-start_epoch<=86400 else 300) if resolution=='auto' else int(resolution)
    first=math.floor(start_epoch/step)*step
    size=math.ceil((end_epoch-first)/step)
    if size>2048: raise ValueError('range exceeds 2048 buckets at this resolution')
    with closing(open_reader(path)) as connection:
        rows=connection.execute('SELECT CAST((received_at-?)/? AS INTEGER) AS bucket,COUNT(*),MIN(temperature_c),MAX(temperature_c),AVG(temperature_c),COUNT(temperature_c),MIN(humidity_pct),MAX(humidity_pct),AVG(humidity_pct),COUNT(humidity_pct) FROM readings WHERE received_at>=? AND received_at<? GROUP BY bucket',(first,step,start_epoch,end_epoch)).fetchall()
    mapping={row[0]:row for row in rows}
    buckets=[]
    for index in range(size):
        row=mapping.get(index)
        buckets.append({'started_at':iso_timestamp(first+index*step),'ended_at':iso_timestamp(first+(index+1)*step),
            'count':row[1] if row else 0,
            'temperature_c':{'min':row[2],'max':row[3],'avg':row[4],'count':row[5]} if row and row[5] else None,
            'humidity_pct':{'min':row[6],'max':row[7],'avg':row[8],'count':row[9]} if row and row[9] else None})
    return {'device_id':device_id,'start':iso_timestamp(start_epoch),'end':iso_timestamp(end_epoch),
            'resolution_seconds':step,'buckets':buckets}


def open_export(path,start,end):
    first,last=range_bounds(start,end)
    connection=open_reader(path)
    try:
        connection.execute('BEGIN')
        count=connection.execute('SELECT COUNT(*) FROM readings WHERE received_at>=? AND received_at<?',(first,last)).fetchone()[0]
        if count>130000: raise ValueError('CSV export exceeds 130000 rows')
        cursor=connection.execute('SELECT device_id,boot_id,seq,temperature_c,humidity_pct,received_at FROM readings WHERE received_at>=? AND received_at<? ORDER BY received_at,id',(first,last))
        return connection,cursor
    except Exception:
        connection.close()
        raise
