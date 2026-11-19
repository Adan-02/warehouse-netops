"""Compatibility entrance. Use python -m virtualization.bootstrap.provision_staging_guests."""
import sys
from pathlib import Path
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from virtualization.bootstrap.provision_staging_guests import main, connect, exec_collect, relay, ADDRESSES

if __name__ == '__main__':
    main()
