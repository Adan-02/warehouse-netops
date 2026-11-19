"""Compatibility entrance. Use python -m virtualization.bootstrap.install_router_packages."""
import sys
from pathlib import Path
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from virtualization.bootstrap.install_router_packages import main, backup_feeds, restore_feeds, connect

if __name__ == '__main__':
    main()
