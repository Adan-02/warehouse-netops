"""Transactional telemetry, process health and durable incident transitions."""
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .config import defaults
from .history import aggregate_history, iso_timestamp, open_export, open_reader, range_bounds, recent_readings
from .migrations import SCHEMA_VERSION, migrate_database
from .thresholds import evaluate_threshold, sampling_interrupted

DEVICE_ID='cabinet-01'
TOPIC='warehouse/cabinet-01/telemetry'
LOG=logging.getLogger('warehouse')


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


class TelemetryService:
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
        self.mqtt_subscription_active=False
        self.storage_state='healthy'
        self.last_persist_error_at=None
        self.last_persist_error=None
        self.last_persist_success_at=None
        self.counters={key:0 for key in ('accepted','rejected','duplicates','conflicts','out_of_order','closed_session','gaps','persist_errors')}
        self._active={}
        self._pending=[]
        self._pending_lost=0
        self._threshold_pending={}
        self._threshold_sample_gap=False
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
        migrate_database(self.db)

    def _interrupt_old_events(self):
        now=self.clock()
        rows=self.db.execute('SELECT event_id,event_type,rule_id,started_at,detected_at,evidence_json FROM events WHERE ended_at IS NULL').fetchall()
        for identity,kind,rule,started,detected,evidence in rows:
            event={'event_id':identity,'event_type':kind,'device_id':DEVICE_ID,'rule_id':rule,
                'started_at':started,'detected_at':detected,'ended_at':now,
                'end_reason':'observation_interrupted','evidence':json.loads(evidence)}
            self._queue(event,'interrupted',now)
        self._flush_events()

    @property
    def broker_connected(self):
        """Compatibility name: true only after the telemetry SUBACK is accepted."""
        return self.mqtt_subscription_active

    @broker_connected.setter
    def broker_connected(self,connected):
        self.set_mqtt_subscription_active(connected)

    def set_mqtt_subscription_active(self,active):
        with self.lock:
            self.mqtt_subscription_active=bool(active)

    def set_broker_connected(self,connected):
        """Retained for old callers; a transport connection alone is insufficient."""
        self.set_mqtt_subscription_active(connected)

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
            previous_received_at=self._latest['received_at'] if self._latest else None
            self._threshold_sample_gap |= sampling_interrupted(previous_received_at,now,self.offline_after_seconds)
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
            return {'device_id':DEVICE_ID,'status':data if self.mqtt_subscription_active else 'broker_disconnected',
                'data_status':data,'broker_connected':self.mqtt_subscription_active,
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
            result['recovery_observation']='available' if (current['data_status']=='online' and self.mqtt_subscription_active and self.storage_state=='healthy') else 'unknown'
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
            self._condition('broker_subscription_unavailable',not self.mqtt_subscription_active,{'broker_connected':self.mqtt_subscription_active})
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
        sample_fresh=(alerts['enabled'] and self.mqtt_subscription_active
                      and status['data_status']=='online' and self.storage_state=='healthy')
        now_monotonic=self.monotonic()
        now_utc=self.clock()
        for field in ('temperature_c','humidity_pct'):
            for direction in ('low','high'):
                rule=field+'_'+direction
                key=('threshold_exceeded',rule)
                limits=alerts[field]
                active=key in self._active
                decision=evaluate_threshold(
                    value=status.get(field),direction=direction,limits=limits,active=active,
                    pending=self._threshold_pending.get(rule),sample_fresh=sample_fresh,
                    now_monotonic=now_monotonic,now_utc=now_utc,
                    trigger_seconds=alerts['trigger_seconds'],recovery_seconds=alerts['recovery_seconds'],
                    sample_gap=self._threshold_sample_gap)
                if decision.pending is None:
                    self._threshold_pending.pop(rule,None)
                else:
                    self._threshold_pending[rule]=decision.pending
                if decision.action in ('raise','recover'):
                    duration=alerts['trigger_seconds'] if decision.action=='raise' else alerts['recovery_seconds']
                    self._condition('threshold_exceeded',decision.action=='raise',
                        {'field':field,'direction':direction,'value':status[field],'limits':dict(limits),
                         'profile':self.config['profile'],'continuous_seconds':duration},rule=rule,
                        started_at=decision.started_at)
        self._threshold_sample_gap=False

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

    def history(self,limit=120,start=None,end=None):
        with self.lock:
            return recent_readings(self.db,limit,start,end)

    def schema_info(self):
        return {'schema_version':SCHEMA_VERSION,'minimum_reader_version':'0.3.0'}

    def _reader(self):
        return open_reader(self.path)

    def history_range(self,start,end,resolution='auto'):
        return aggregate_history(self.path,DEVICE_ID,start,end,resolution)

    def open_export(self,start,end):
        return open_export(self.path,start,end)

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


# Existing integrations may still import the original storage-oriented name.
TelemetryStore = TelemetryService
