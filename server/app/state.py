"""Transactional telemetry, process health and durable incident transitions."""
import json
from contextlib import closing
import logging
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from datetime import datetime,timezone
import uuid

from .config import defaults

DEVICE_ID='cabinet-01'
TOPIC='warehouse/cabinet-01/telemetry'
SCHEMA_VERSION=3
LOG=logging.getLogger('warehouse')


def iso_timestamp(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value is not None else None


def finite_number(value,field):
    if type(value) not in (int,float):
        raise ValueError(field+' must be finite')
    try:
        result=float(value)
    except OverflowError as error:
        raise ValueError(field+' must be finite') from error
    if not math.isfinite(result):
        raise ValueError(field+' must be finite')
    return result


def parse_packet(payload):
    if len(payload)>4096:
        raise ValueError('payload exceeds 4096 bytes')
    try:
        value=json.loads(payload)
    except (ValueError,UnicodeError) as error:
        raise ValueError('invalid JSON') from error
    if not isinstance(value,dict) or value.get('device_id')!=DEVICE_ID:
        raise ValueError('unexpected device_id')
    seq=value.get('seq')
    if type(seq) is not int or not 1<=seq<=2**53-1:
        raise ValueError('seq must be a positive JSON-safe integer')
    temperature=finite_number(value.get('temperature_c'),'temperature_c')
    humidity=None
    if 'humidity_pct' in value:
        humidity=finite_number(value['humidity_pct'],'humidity_pct')
        if not 0<=humidity<=100:
            raise ValueError('humidity_pct must be between 0 and 100')
    boot=value.get('boot_id')
    if 'boot_id' in value and (not isinstance(boot,str) or not re.fullmatch('[0-9a-f]{32}',boot)):
        raise ValueError('boot_id must be 32 lowercase hexadecimal characters')
    return {'seq':seq,'temperature_c':temperature,'humidity_pct':humidity,'boot_id':boot}


def parse_reading(payload):
    """Keep the original parsing interface available to earlier callers."""
    value=parse_packet(payload)
    return value['seq'],value['temperature_c'],value['humidity_pct']


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


class TelemetryStore:
    def __init__(self,db_path,offline_after_seconds=20,clock=time.time,monotonic=time.monotonic,config=None):
        self.path=str(db_path)
        Path(db_path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(db_path,check_same_thread=False,timeout=2)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.lock=threading.RLock()
        self.clock=clock
        self.monotonic=monotonic
        self.config=config or defaults()
        self.offline_after_seconds=offline_after_seconds
        self.broker_connected=False
        self.storage_state='healthy'
        self.last_persist_error_at=None
        self.last_persist_error=None
        self.last_persist_success_at=None
        self.counters={key:0 for key in ('accepted','rejected','duplicates','conflicts','out_of_order','closed_session','gaps','persist_errors')}
        self._active={}
        self._pending=[]
        self._pending_lost=0
        self._threshold_pending={}
        # First background tick also performs retention; frequent restarts must
        # not postpone cleanup indefinitely.
        self._last_cleanup=monotonic()-86400
        self._stop=threading.Event()
        self._thread=None
        self._closed=False
        self._migrate()
        row=self.db.execute('SELECT seq,temperature_c,humidity_pct,received_at,boot_id FROM readings ORDER BY id DESC LIMIT 1').fetchone()
        self._latest=({'seq':row[0],'temperature_c':row[1],'humidity_pct':row[2],
                      'received_at':row[3],'boot_id':row[4]} if row else None)
        self.last_persist_success_at=row[3] if row else None
        self._interrupt_old_events()

    def _migrate(self):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY,seq INTEGER NOT NULL,temperature_c REAL NOT NULL,received_at REAL NOT NULL,humidity_pct REAL)')
            columns={row[1] for row in self.db.execute('PRAGMA table_info(readings)')}
            for name,kind in (('humidity_pct','REAL'),('device_id',"TEXT DEFAULT 'cabinet-01'"),('boot_id','TEXT')):
                if name not in columns:
                    self.db.execute('ALTER TABLE readings ADD COLUMN '+name+' '+kind)
            self.db.execute('CREATE INDEX IF NOT EXISTS readings_received_at ON readings(received_at)')
            self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS readings_identity ON readings(device_id,boot_id,seq) WHERE boot_id IS NOT NULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS device_sessions (device_id TEXT NOT NULL,boot_id TEXT NOT NULL,first_seen_at REAL NOT NULL,last_seen_at REAL NOT NULL,max_seq INTEGER NOT NULL,closed_at REAL,PRIMARY KEY(device_id,boot_id))')
            self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_current_session ON device_sessions(device_id) WHERE closed_at IS NULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,device_id TEXT NOT NULL,rule_id TEXT,started_at REAL NOT NULL,detected_at REAL NOT NULL,ended_at REAL,end_reason TEXT,evidence_json TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS event_changes (change_id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL,transition TEXT NOT NULL,occurred_at REAL NOT NULL,event_json TEXT NOT NULL)')
            self.db.execute('CREATE INDEX IF NOT EXISTS events_ended_at ON events(ended_at)')
            self.db.execute('CREATE TABLE IF NOT EXISTS schema_meta (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
            existing=self.db.execute("SELECT value FROM schema_meta WHERE name='schema_version'").fetchone()
            if existing and int(existing[0])>SCHEMA_VERSION:
                raise ValueError('Database schema is newer than this application')
            self.db.execute("INSERT INTO schema_meta(name,value) VALUES ('schema_version',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(str(SCHEMA_VERSION),))
            self.db.execute("INSERT INTO schema_meta(name,value) VALUES ('minimum_reader_version','0.3.0') ON CONFLICT(name) DO NOTHING")
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def _interrupt_old_events(self):
        now=self.clock()
        rows=self.db.execute('SELECT event_id,event_type,rule_id,started_at,detected_at,evidence_json FROM events WHERE ended_at IS NULL').fetchall()
        for identity,kind,rule,started,detected,evidence in rows:
            event={'event_id':identity,'event_type':kind,'device_id':DEVICE_ID,'rule_id':rule,
                'started_at':started,'detected_at':detected,'ended_at':now,
                'end_reason':'observation_interrupted','evidence':json.loads(evidence)}
            self._queue(event,'interrupted',now)
        self._flush_events()

    def set_broker_connected(self,connected):
        with self.lock:
            self.broker_connected=bool(connected)

    def ingest(self,payload,retained=False):
        try:
            if retained: raise ValueError('retained telemetry is not a live heartbeat')
            value=parse_packet(payload)
        except ValueError:
            with self.lock: self.counters['rejected']+=1
            raise
        with self.lock:
            now=self.clock()
            boot=value['boot_id']
            gap=0
            try:
                self.db.execute('BEGIN IMMEDIATE')
                if boot is not None:
                    duplicate=self.db.execute('SELECT temperature_c,humidity_pct FROM readings WHERE device_id=? AND boot_id=? AND seq=?',(DEVICE_ID,boot,value['seq'])).fetchone()
                    if duplicate:
                        self.db.rollback()
                        if duplicate==(value['temperature_c'],value['humidity_pct']):
                            self.counters['duplicates']+=1
                            return {'accepted':False,'reason':'duplicate'}
                        self.counters['conflicts']+=1
                        self.counters['rejected']+=1
                        raise ValueError('conflicting telemetry identity')
                    session=self.db.execute('SELECT max_seq,closed_at FROM device_sessions WHERE device_id=? AND boot_id=?',(DEVICE_ID,boot)).fetchone()
                    if session and session[1] is not None:
                        self.db.rollback()
                        self.counters['closed_session']+=1
                        return {'accepted':False,'reason':'closed_session'}
                    if session and value['seq']<=session[0]:
                        self.db.rollback()
                        self.counters['out_of_order']+=1
                        return {'accepted':False,'reason':'out_of_order'}
                    if session:
                        gap=max(0,value['seq']-session[0]-1)
                        self.db.execute('UPDATE device_sessions SET max_seq=?,last_seen_at=? WHERE device_id=? AND boot_id=?',(value['seq'],now,DEVICE_ID,boot))
                    else:
                        self.db.execute('UPDATE device_sessions SET closed_at=? WHERE device_id=? AND closed_at IS NULL',(now,DEVICE_ID))
                        self.db.execute('INSERT INTO device_sessions(device_id,boot_id,first_seen_at,last_seen_at,max_seq) VALUES (?,?,?,?,?)',(DEVICE_ID,boot,now,now,value['seq']))
                self.db.execute('INSERT INTO readings(seq,temperature_c,humidity_pct,received_at,device_id,boot_id) VALUES (?,?,?,?,?,?)',
                    (value['seq'],value['temperature_c'],value['humidity_pct'],now,DEVICE_ID,boot))
                self.db.commit()
            except ValueError:
                raise
            except Exception as error:
                try: self.db.rollback()
                except sqlite3.Error: pass
                self._storage_failure(error)
                raise
            # A new sample can arrive before the background evaluator runs
            # again (for example after a suspended VM resumes). Do not count
            # that unobserved gap as continuous threshold/recovery evidence.
            if self._latest and now-self._latest['received_at']>=self.offline_after_seconds:
                self._threshold_pending.clear()
            self._latest=dict(value,received_at=now)
            self.last_persist_success_at=now
            self.storage_state='healthy'
            self.counters['accepted']+=1
            self.counters['gaps']+=gap
            return {'accepted':True,'reason':'stored'}

    def _storage_failure(self,error):
        self.storage_state='unhealthy'
        self.last_persist_error_at=self.clock()
        self.last_persist_error=type(error).__name__
        self.counters['persist_errors']+=1
        LOG.error(json.dumps({'kind':'storage_write_failed','error_type':type(error).__name__,
            'observed_at':iso_timestamp(self.clock())},separators=(',',':')))
        self._condition('storage_write_failed',True,{'error_type':type(error).__name__})

    def status(self):
        """Use the last committed snapshot; diagnosis survives a bad database."""
        with self.lock:
            row=self._latest
            now=self.clock()
            age=max(0,now-row['received_at']) if row else None
            data='awaiting_data' if not row else 'online' if age<self.offline_after_seconds else 'stale'
            return {'device_id':DEVICE_ID,'status':data if self.broker_connected else 'broker_disconnected',
                'data_status':data,'broker_connected':self.broker_connected,
                'seq':row['seq'] if row else None,'temperature_c':row['temperature_c'] if row else None,
                'humidity_pct':row['humidity_pct'] if row else None,'boot_id':row['boot_id'] if row else None,
                'dedup_available':bool(row and row['boot_id']),
                'last_seen_at':iso_timestamp(row['received_at']) if row else None,
                'age_seconds':round(age,1) if age is not None else None,
                'offline_after_seconds':self.offline_after_seconds,'observed_at':iso_timestamp(now),
                'storage_state':self.storage_state,'last_persist_success_at':iso_timestamp(self.last_persist_success_at),
                'last_persist_error_at':iso_timestamp(self.last_persist_error_at),
                'last_persist_error':self.last_persist_error,'counters':dict(self.counters),
                'counter_scope':'current_process','event_buffer_lost':self._pending_lost,
                'alert_profile':self.config['profile'],'alerts_enabled':self.config['alerts']['enabled']}

    def _public_event(self,event):
        result={**{key:value for key,value in event.items() if key not in ('started_at','detected_at','ended_at')},
            'started_at':iso_timestamp(event['started_at']),'detected_at':iso_timestamp(event['detected_at']),
            'ended_at':iso_timestamp(event.get('ended_at')),
            'state':'active' if event.get('ended_at') is None else 'interrupted' if event.get('end_reason')=='observation_interrupted' else 'resolved'}
        if result['state']=='active' and event['event_type']=='threshold_exceeded':
            current=self.status()
            result['recovery_observation']='available' if (current['data_status']=='online' and self.broker_connected and self.storage_state=='healthy') else 'unknown'
        return result

    def live_events(self):
        with self.lock:
            return {'active_events':[self._public_event(event) for event in self._active.values()],
                    'events_pending_persistence':len(self._pending),'event_buffer_lost':self._pending_lost}

    def _queue(self,event,transition,when):
        item=(dict(event),transition,when)
        if len(self._pending)>=1024:
            self._pending.pop(0)
            self._pending_lost+=1
        self._pending.append(item)
        LOG.warning(json.dumps({'kind':'event_transition','transition':transition,
            'event':self._public_event(event),'persisted':False},separators=(',',':')))

    def _condition(self,kind,failed,evidence=None,rule=None,started_at=None):
        key=(kind,rule)
        now=self.clock()
        if failed and key not in self._active:
            event={'event_id':uuid.uuid4().hex,'event_type':kind,'device_id':DEVICE_ID,'rule_id':rule,
                'started_at':now if started_at is None else started_at,'detected_at':now,
                'ended_at':None,'end_reason':None,'evidence':evidence or {}}
            self._active[key]=event
            self._queue(event,'raised',now)
        elif not failed and key in self._active:
            event=self._active.pop(key)
            event=dict(event,ended_at=now,end_reason='observed_recovery',
                       evidence={**event['evidence'],'recovery':evidence or {}})
            self._queue(event,'recovered',now)

    def _flush_events(self):
        if not self._pending: return
        try:
            self.db.execute('BEGIN IMMEDIATE')
            for event,transition,when in self._pending:
                self.db.execute('INSERT INTO events(event_id,event_type,device_id,rule_id,started_at,detected_at,ended_at,end_reason,evidence_json) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET ended_at=excluded.ended_at,end_reason=excluded.end_reason,evidence_json=excluded.evidence_json',
                    (event['event_id'],event['event_type'],event['device_id'],event['rule_id'],event['started_at'],event['detected_at'],event.get('ended_at'),event.get('end_reason'),json.dumps(event['evidence'],separators=(',',':'))))
                self.db.execute('INSERT INTO event_changes(event_id,transition,occurred_at,event_json) VALUES (?,?,?,?)',
                    (event['event_id'],transition,when,json.dumps(self._public_event(event),separators=(',',':'))))
            self.db.commit()
            self._pending.clear()
        except sqlite3.Error as error:
            self.db.rollback()
            if self.storage_state!='unhealthy': self._storage_failure(error)

    def tick(self):
        with self.lock:
            status=self.status()
            self._condition('broker_subscription_unavailable',not self.broker_connected,{'broker_connected':self.broker_connected})
            self._condition('telemetry_stale',status['data_status']=='stale',
                {'last_seen_at':status['last_seen_at'],'offline_after_seconds':self.offline_after_seconds},
                started_at=self._latest['received_at']+self.offline_after_seconds if self._latest else None)
            self._condition('storage_write_failed',self.storage_state=='unhealthy',{'error_type':self.last_persist_error})
            self._evaluate_thresholds(status)
            self._flush_events()
            if self.monotonic()-self._last_cleanup>=86400:
                if self._cleanup(): self._last_cleanup=self.monotonic()

    def _evaluate_thresholds(self,status):
        alerts=self.config['alerts']
        ready=alerts['enabled'] and self.broker_connected and status['data_status']=='online' and self.storage_state=='healthy'
        now=self.monotonic()
        for field in ('temperature_c','humidity_pct'):
            for direction in ('low','high'):
                rule=field+'_'+direction
                key=('threshold_exceeded',rule)
                value=status.get(field)
                if not ready or value is None:
                    self._threshold_pending.pop(rule,None)
                    continue
                limits=alerts[field]
                violated=value<limits['min'] if direction=='low' else value>limits['max']
                recovered=value>=limits['min']+limits['hysteresis'] if direction=='low' else value<=limits['max']-limits['hysteresis']
                active=key in self._active
                qualifies=recovered if active else violated
                if not qualifies:
                    self._threshold_pending.pop(rule,None)
                    continue
                pending=self._threshold_pending.get(rule)
                if pending is None or pending[1]!=active:
                    pending=(now,active,self.clock())
                    self._threshold_pending[rule]=pending
                duration=alerts['recovery_seconds'] if active else alerts['trigger_seconds']
                if now-pending[0]>=duration:
                    self._condition('threshold_exceeded',not active,
                        {'field':field,'direction':direction,'value':value,'limits':dict(limits),
                         'profile':self.config['profile'],'continuous_seconds':duration},rule=rule,
                         started_at=pending[2] if not active else None)
                    self._threshold_pending.pop(rule,None)

    def events(self,after_id=None,limit=50):
        with self.lock:
            select='SELECT id,event_id,event_type,device_id,rule_id,started_at,detected_at,ended_at,end_reason,evidence_json FROM events'
            if after_id is None:
                rows=list(reversed(self.db.execute(select+' ORDER BY id DESC LIMIT ?',(limit,)).fetchall()))
            else:
                rows=self.db.execute(select+' WHERE id>? ORDER BY id LIMIT ?',(after_id,limit)).fetchall()
        result=[]
        for row in rows:
            event=dict(zip(('id','event_id','event_type','device_id','rule_id','started_at','detected_at','ended_at','end_reason'),row[:9]))
            event['evidence']=json.loads(row[9])
            result.append(self._public_event(event))
        return {'events':result,'next_after_id':rows[-1][0] if rows else after_id or 0}

    def event_changes(self,after_id=0,limit=200):
        with self.lock:
            rows=self.db.execute('SELECT change_id,event_id,transition,occurred_at,event_json FROM event_changes WHERE change_id>? ORDER BY change_id LIMIT ?',(after_id,limit)).fetchall()
            latest=self.db.execute("SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='event_changes'),0)").fetchone()[0]
        return {'changes':[{'change_id':row[0],'event_id':row[1],'transition':row[2],
                            'occurred_at':iso_timestamp(row[3]),'event':json.loads(row[4])} for row in rows],
                'next_after_id':rows[-1][0] if rows else after_id,'latest_change_id':latest}

    def history(self,limit=120):
        with self.lock:
            rows=self.db.execute('SELECT seq,temperature_c,humidity_pct,received_at FROM readings ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
        return [{'seq':seq,'temperature_c':temperature,'humidity_pct':humidity,'received_at':iso_timestamp(received)}
                for seq,temperature,humidity,received in reversed(rows)]

    def schema_info(self):
        return {'schema_version':SCHEMA_VERSION,'minimum_reader_version':'0.3.0'}

    def _reader(self):
        # Separate read-only connections keep exports outside the ingestion lock.
        target=Path(self.path).resolve().as_uri()+'?mode=ro'
        connection=sqlite3.connect(target,uri=True,timeout=2)
        connection.execute('PRAGMA query_only=ON')
        return connection

    def history_range(self,start,end,resolution='auto'):
        start_epoch,end_epoch=range_bounds(start,end)
        if resolution not in ('auto','60','300'):
            raise ValueError('resolution must be auto, 60 or 300')
        step=(60 if end_epoch-start_epoch<=86400 else 300) if resolution=='auto' else int(resolution)
        first=math.floor(start_epoch/step)*step
        size=math.ceil((end_epoch-first)/step)
        if size>2048: raise ValueError('range exceeds 2048 buckets at this resolution')
        with closing(self._reader()) as connection:
            rows=connection.execute('SELECT CAST((received_at-?)/? AS INTEGER) AS bucket,COUNT(*),MIN(temperature_c),MAX(temperature_c),AVG(temperature_c),COUNT(temperature_c),MIN(humidity_pct),MAX(humidity_pct),AVG(humidity_pct),COUNT(humidity_pct) FROM readings WHERE received_at>=? AND received_at<? GROUP BY bucket',(first,step,start_epoch,end_epoch)).fetchall()
        mapping={row[0]:row for row in rows}
        buckets=[]
        for index in range(size):
            row=mapping.get(index)
            buckets.append({'started_at':iso_timestamp(first+index*step),'ended_at':iso_timestamp(first+(index+1)*step),
                'count':row[1] if row else 0,
                'temperature_c':{'min':row[2],'max':row[3],'avg':row[4],'count':row[5]} if row and row[5] else None,
                'humidity_pct':{'min':row[6],'max':row[7],'avg':row[8],'count':row[9]} if row and row[9] else None})
        return {'device_id':DEVICE_ID,'start':iso_timestamp(start_epoch),'end':iso_timestamp(end_epoch),
                'resolution_seconds':step,'buckets':buckets}

    def open_export(self,start,end):
        first,last=range_bounds(start,end)
        connection=self._reader()
        try:
            connection.execute('BEGIN')
            count=connection.execute('SELECT COUNT(*) FROM readings WHERE received_at>=? AND received_at<?',(first,last)).fetchone()[0]
            if count>130000: raise ValueError('CSV export exceeds 130000 rows')
            cursor=connection.execute('SELECT device_id,boot_id,seq,temperature_c,humidity_pct,received_at FROM readings WHERE received_at>=? AND received_at<? ORDER BY received_at,id',(first,last))
            return connection,cursor
        except Exception:
            connection.close()
            raise

    def _cleanup(self):
        """Bounded daily batches; no vacuum and no deletion of active incidents."""
        try:
            now=self.clock()
            cutoff=now-self.config['retention']['events_days']*86400
            # Up to 20 short transactions clear more than a day's 5s samples.
            # A larger backlog remains due and is resumed on the next tick.
            for _ in range(20):
                self.db.execute('BEGIN IMMEDIATE')
                deleted=self.db.execute('DELETE FROM readings WHERE id IN (SELECT id FROM readings WHERE received_at<? ORDER BY id LIMIT 1000)',
                    (now-self.config['retention']['readings_days']*86400,)).rowcount
                expired=self.db.execute('SELECT event_id FROM events WHERE ended_at IS NOT NULL AND ended_at<? ORDER BY id LIMIT 1000',(cutoff,)).fetchall()
                for (identity,) in expired:
                    self.db.execute('DELETE FROM event_changes WHERE event_id=?',(identity,))
                    self.db.execute('DELETE FROM events WHERE event_id=?',(identity,))
                self.db.commit()
                if deleted<1000 and len(expired)<1000: return True
            return False
        except sqlite3.Error as error:
            self.db.rollback()
            self._storage_failure(error)
            return False

    def start_background(self):
        if self._thread is not None: return
        def run():
            while not self._stop.wait(1):
                try: self.tick()
                except Exception: LOG.exception('Background event assessment failed')
        self._thread=threading.Thread(target=run,name='warehouse-events',daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread(): self._thread.join(timeout=5)
        with self.lock:
            if not self._closed:
                self.db.close()
                self._closed=True
