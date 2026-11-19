"""One directory contract for observer code, private state and public snapshots.

Installed keys and outbox stay in PRIVATE_DIR. ``state_dir`` is the old name
for that directory and remains a supported configuration alias.
"""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

BASE_DIR = Path('/var/lib/warehouse-netops-observer')
PRIVATE_DIR = BASE_DIR/'private'
OBSERVATIONS_DIR = BASE_DIR/'observations'
CODE_DIR = Path('/opt/warehouse-netops-observer')


def directory(value, name):
    if not isinstance(value, str) or not value or any(c in value for c in ('\x00', '\n', '\r')):
        raise ValueError(name+' must be an absolute local directory')
    path = PurePosixPath(value) if value.startswith('/') else Path(value)
    if not path.is_absolute():
        raise ValueError(name+' must be an absolute local directory')
    return path


@dataclass(frozen=True)
class ObserverPaths:
    private_dir: str
    observations_dir: str


def observer_paths(config):
    private = config.get('private_dir', config.get('state_dir', PRIVATE_DIR.as_posix()))
    legacy = config.get('state_dir')
    if legacy is not None and 'private_dir' in config and str(directory(legacy, 'state_dir')) != str(directory(private, 'private_dir')):
        raise ValueError('private_dir and legacy state_dir disagree; no files were moved')
    observations = config.get('observations_dir', OBSERVATIONS_DIR.as_posix())
    return ObserverPaths(str(directory(private, 'private_dir')), str(directory(observations, 'observations_dir')))


def observer_id(role):
    """The old cloud CLI/unit role maps to the existing guangzhou wire identity."""
    if role == 'cloud':
        return 'guangzhou'
    if role not in ('site', 'guangzhou'):
        raise ValueError('Unknown observer location')
    return role


def unit_role(role):
    return 'cloud' if observer_id(role) == 'guangzhou' else 'site'
