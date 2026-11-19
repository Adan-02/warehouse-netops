"""Durable event cursor/outbox and signed, bounded outbound notifications."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import time
from urllib.parse import urlsplit
import uuid

from .common import _bounded_value, iso_time

MAX_ATTEMPTS = 8
MAX_BODY = 65536


def destination(config):
    if not config.get('enabled', False):
        return None
    endpoint = config.get('url', '')
    secret = config.get('secret', '')
    if not isinstance(endpoint, str) or not isinstance(secret, str) or len(secret) < 16:
        raise ValueError('Enabled Webhook requires a URL and a signing secret of at least 16 characters')
    url = urlsplit(endpoint)
    if not url.hostname or url.username is not None or url.password is not None or url.fragment:
        raise ValueError('Webhook URL must not contain credentials or fragments')
    test_mode = config.get('allow_loopback_http_for_tests') is True
    loopback = url.hostname == 'localhost'
    try:
        loopback = ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        pass
    if url.scheme != 'https' and not (test_mode and loopback and url.scheme == 'http'):
        raise ValueError('Webhook requires verified HTTPS; test HTTP is limited to explicit loopback mode')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Invalid Webhook port')
    return {'url': url, 'secret': secret.encode(), 'identity': hashlib.sha256(endpoint.encode()).hexdigest()}


def send(payload, delivery_id, config):
    """Return only status/retry metadata. URLs and secrets never enter results."""
    target = destination(config)
    if target is None:
        return {'state': 'disabled'}
    body = payload.encode('utf-8') if isinstance(payload, str) else payload
    if len(body) > MAX_BODY:
        return {'state': 'failed', 'reason': 'payload_too_large'}
    signature = hmac.new(target['secret'], body, hashlib.sha256).hexdigest()
    url = target['url']
    port = url.port or (443 if url.scheme == 'https' else 80)
    cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    options = {'timeout': 3}
    if url.scheme == 'https':
        options['context'] = ssl.create_default_context()
    connection = cls(url.hostname, port, **options)
    try:
        connection.request('POST', (url.path or '/') + (('?' + url.query) if url.query else ''), body=body,
            headers={'Content-Type': 'application/json', 'Idempotency-Key': delivery_id,
                'X-Warehouse-Signature': 'sha256=' + signature,
                'User-Agent': 'warehouse-netops-webhook/1'})
        response = connection.getresponse()
        # Neither response bodies nor redirects are followed or logged.
        code = response.status
        retry_after = response.getheader('Retry-After', '')
        response.read(4097)
        if 200 <= code < 300:
            return {'state': 'sent', 'http_status': code}
        if code == 429 or code >= 500:
            delay = min(3600, max(0, int(retry_after))) if retry_after.isdecimal() else None
            return {'state': 'retry', 'http_status': code, 'retry_after': delay}
        return {'state': 'failed', 'http_status': code, 'reason': 'http_rejected'}
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError) as error:
        return {'state': 'retry', 'reason': type(error).__name__}
    finally:
        connection.close()


def notification(source, event_id, transition, occurred_at, event, change_id=None):
    if transition not in ('raised', 'recovered', 'interrupted'):
        raise ValueError('Invalid event transition')
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
        raise ValueError('Invalid event identity')
    if not isinstance(event,dict):
        raise ValueError('Notification event must be an object')
    _bounded_value(event)
    identity = '|'.join((source, event_id, transition, str(change_id or '')))
    delivery_id = hashlib.sha256(identity.encode()).hexdigest()
    value = {'delivery_id': delivery_id, 'source': source, 'event_id': event_id,
        'transition': transition, 'occurred_at': occurred_at, 'event': event}
    return delivery_id, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class DeliveryStore:
    def __init__(self, path, clock=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError('Refusing notification database symlink')
        self.path = path
        self.clock = clock
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(
 delivery_id TEXT PRIMARY KEY,payload TEXT NOT NULL,destination_id TEXT NOT NULL,
 status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL,
 created_at REAL NOT NULL,sent_at REAL,last_result TEXT);
CREATE TABLE IF NOT EXISTS remote_events(
 event_id TEXT PRIMARY KEY,started_at REAL NOT NULL,ended_at REAL,state TEXT NOT NULL);
''')
        self.db.commit()

    def close(self):
        self.db.close()

    def _get(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key, value):
        self.db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    @property
    def cursor(self):
        return int(self._get('site_cursor', '0'))

    def initialized(self, webhook):
        target = destination(webhook)
        return target is not None and self._get('enabled_destination') == target['identity']

    def initialize(self, latest_change_id, webhook):
        if type(latest_change_id) is not int or latest_change_id < 0:
            raise ValueError('Invalid latest event cursor')
        target = destination(webhook)
        with self.db:
            self._set('site_cursor', latest_change_id)
            self._set('enabled_destination', target['identity'] if target else '')
        # Initial/changed destination starts at the current cursor; old failures
        # are retained for inspection and never silently sent to a new endpoint.

    def _enqueue(self, identity, payload, target):
        if len(payload.encode()) > MAX_BODY:
            raise ValueError('Notification exceeds bounded size')
        self.db.execute('INSERT OR IGNORE INTO deliveries(delivery_id,payload,destination_id,status,next_attempt_at,created_at) VALUES(?,?,?,?,?,?)',
            (identity, payload, target['identity'], 'pending', self.clock(), self.clock()))

    def accept_changes(self, value, webhook):
        target = destination(webhook)
        latest = value.get('latest_change_id')
        rows = value.get('changes')
        if type(latest) is not int or latest < 0 or not isinstance(rows, list) or len(rows) > 200:
            raise ValueError('Invalid event change response')
        if target is None or not self.initialized(webhook):
            self.initialize(latest, webhook)
            return 0
        cursor = self.cursor
        accepted = 0
        with self.db:
            for row in rows:
                change_id = row.get('change_id')
                if type(change_id) is not int or not cursor < change_id <= latest:
                    raise ValueError('Event changes must be ordered after the durable cursor')
                identity, payload = notification('site', row.get('event_id'), row.get('transition'), row.get('occurred_at'), row.get('event'), change_id)
                self._enqueue(identity, payload, target)
                cursor = change_id
                accepted += 1
            advertised = value.get('next_after_id', cursor)
            if advertised != cursor:
                raise ValueError('Event page cursor differs from accepted rows')
            self._set('site_cursor', cursor)
        return accepted

    def observe_remote(self, reachable, webhook):
        """Persist cloud-side unreachability independently of the site DB."""
        if type(reachable) is not bool:
            raise ValueError('Remote reachability must be a Boolean observation')
        target = destination(webhook)
        active = self.db.execute("SELECT event_id,started_at FROM remote_events WHERE state='active' ORDER BY started_at DESC LIMIT 1").fetchone()
        now = self.clock()
        with self.db:
            if not reachable and not active:
                identity = uuid.uuid4().hex
                self.db.execute('INSERT INTO remote_events VALUES(?,?,NULL,?)', (identity, now, 'active'))
                if target:
                    delivery, payload = notification('guangzhou', identity, 'raised', iso_time(now), {'event_type': 'remote_service_unreachable', 'started_at': iso_time(now), 'evidence': {'observer': 'guangzhou', 'target': 'service-http'}})
                    self._enqueue(delivery, payload, target)
            elif reachable and active:
                self.db.execute("UPDATE remote_events SET state='closed',ended_at=? WHERE event_id=?", (now, active[0]))
                if target:
                    delivery, payload = notification('guangzhou', active[0], 'recovered', iso_time(now), {'event_type': 'remote_service_unreachable', 'started_at': iso_time(active[1]), 'ended_at': iso_time(now), 'duration_seconds': round(now-active[1], 3)})
                    self._enqueue(delivery, payload, target)

    def dispatch(self, webhook, sender=send, maximum=8):
        target = destination(webhook)
        if target is None:
            return {'enabled': False, 'processed': 0, **self.summary()}
        rows = self.db.execute("SELECT delivery_id,payload,attempts FROM deliveries WHERE destination_id=? AND status IN ('pending','retry') AND next_attempt_at<=? ORDER BY created_at,delivery_id LIMIT ?", (target['identity'], self.clock(), maximum)).fetchall()
        for delivery_id, payload, previous in rows:
            result = sender(payload, delivery_id, webhook)
            if result.get('state') not in ('sent','retry','failed'):
                raise ValueError('Notification sender returned an invalid result')
            attempts = previous + 1
            state = result['state']
            if state == 'retry' and attempts >= MAX_ATTEMPTS:
                state = 'failed'
                result['reason'] = 'retry_limit_reached'
            delay = max(min(300, 5 * 2**(attempts-1)), result.get('retry_after') or 0)
            with self.db:
                self.db.execute('UPDATE deliveries SET status=?,attempts=?,next_attempt_at=?,sent_at=?,last_result=? WHERE delivery_id=?',
                    (state, attempts, self.clock()+delay, self.clock() if state=='sent' else None, json.dumps(result, separators=(',', ':')), delivery_id))
        return {'enabled': True, 'processed': len(rows), **self.summary()}

    def summary(self):
        rows = self.db.execute('SELECT status,count(*) FROM deliveries GROUP BY status').fetchall()
        return {'cursor': self.cursor, 'deliveries': dict(rows), 'remote_event_active': self.db.execute("SELECT count(*) FROM remote_events WHERE state='active'").fetchone()[0] > 0}

    def recent_remote_events(self, limit=5):
        if not 1<=limit<=10:raise ValueError('Remote event summary limit is out of range')
        rows=self.db.execute('SELECT event_id,started_at,ended_at,state FROM remote_events ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
        return [{'event_id':identity,'started_at':iso_time(started),'ended_at':iso_time(ended) if ended is not None else None,'state':state}
            for identity,started,ended,state in rows]

    def daily_backup(self, directory, keep=7):
        """One consistent backup per UTC date, private and independent of the site."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError('Refusing backup-directory symlink')
        name = datetime.fromtimestamp(self.clock(), timezone.utc).strftime('outbox-%Y%m%d.sqlite3')
        target = directory/name
        if target.exists():
            if target.is_symlink(): raise ValueError('Refusing backup symlink')
            return target
        with self.db:
            cutoff=self.clock()-180*86400
            self.db.execute("DELETE FROM remote_events WHERE state='closed' AND ended_at<?",(cutoff,))
            self.db.execute("DELETE FROM deliveries WHERE status IN ('sent','failed') AND created_at<?",(cutoff,))
        temporary = directory/(name+'.part')
        if temporary.exists():
            if temporary.is_symlink(): raise ValueError('Refusing backup symlink')
            temporary.unlink()
        copy=sqlite3.connect(temporary)
        try:
            self.db.backup(copy)
            check = copy.execute('PRAGMA integrity_check').fetchone()[0]
            if check != 'ok':
                raise RuntimeError('Notification backup integrity check failed')
        finally:
            copy.close()
        temporary.chmod(0o600)
        os.replace(temporary,target)
        backups = sorted(directory.glob('outbox-[0-9]'+'[0-9]'*7+'.sqlite3'))
        for old in backups[:-keep]:
            if old.is_file() and not old.is_symlink(): old.unlink()
        return target
