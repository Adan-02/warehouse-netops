"""Bounded JSON and timestamp helpers shared by the observer processes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

MAX_SNAPSHOT_BYTES = 32768
TTL_SECONDS = 35
STATES = {'healthy', 'unhealthy', 'unknown'}
COMPONENTS = {'site': {'wan', 'wireguard'}, 'guangzhou': {'wireguard', 'remote_http'}}


def iso_time(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat()


def parse_time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Invalid observation time')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Observation time must carry a timezone')
    return result.timestamp()


def private_config(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Configuration must be a regular file')
    if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Observer configuration must have mode 0600')
    if info.st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError('Configuration exceeds size limit')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Configuration must be an object')
    return value


def _bounded_value(value, depth=0):
    if depth > 6:
        raise ValueError('Observation nesting exceeds limit')
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise ValueError('Non-finite observation number')
        return
    if isinstance(value, str):
        if len(value) > 1024 or 'PRIVATE KEY' in value:
            raise ValueError('Unsafe observation text')
        return
    if isinstance(value, list):
        if len(value) > 64:
            raise ValueError('Observation list exceeds limit')
        for item in value:
            _bounded_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 32:
            raise ValueError('Observation object exceeds limit')
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 64 or any(term in key.lower() for term in ('password', 'private_key', 'token', 'secret', 'command', 'path')):
                raise ValueError('Unsafe observation field')
            _bounded_value(item, depth + 1)
        return
    raise ValueError('Invalid observation value')


def validate_snapshot(value, observer_id, *, now=None, stamp_received=True):
    now = time.time() if now is None else now
    if observer_id not in COMPONENTS or not isinstance(value, dict):
        raise ValueError('Invalid observer identity')
    if set(value) - {'schema_version', 'observer_id', 'observed_at', 'received_at', 'components'}:
        raise ValueError('Unexpected snapshot fields')
    if value.get('schema_version') != 1 or value.get('observer_id') != observer_id:
        raise ValueError('Unexpected snapshot identity or schema')
    epoch = parse_time(value.get('observed_at'))
    if not -5 <= now - epoch <= TTL_SECONDS:
        raise ValueError('Snapshot is stale or in the future')
    components = value.get('components')
    if not isinstance(components, dict) or set(components) != COMPONENTS[observer_id]:
        raise ValueError('Snapshot component set differs')
    result = {'schema_version': 1, 'observer_id': observer_id, 'observed_at': value['observed_at'], 'components': {}}
    for name, row in components.items():
        if not isinstance(row, dict) or set(row) != {'state', 'observed_at', 'evidence'} or row['state'] not in STATES:
            raise ValueError('Invalid component observation')
        sample_time = parse_time(row['observed_at'])
        if not -5 <= now - sample_time <= TTL_SECONDS:
            raise ValueError('Component observation is stale')
        if not isinstance(row['evidence'], dict):
            raise ValueError('Observation evidence must be an object')
        _bounded_value(row['evidence'])
        result['components'][name] = dict(row)
    result['received_at'] = iso_time(now) if stamp_received else value.get('received_at', iso_time(now))
    if len(json.dumps(result, allow_nan=False).encode()) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Snapshot exceeds size limit')
    return result


def atomic_json(path, value, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('Refusing observation symlink')
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError('JSON output exceeds limit')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.observation-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def component(state, evidence, observed_at=None):
    if state not in STATES:
        raise ValueError('Unknown component state')
    return {'state': state, 'observed_at': observed_at or iso_time(), 'evidence': evidence}
