"""Compatibility entrance: python -m virtualization.bootstrap.create_guest_disks."""
import sys
from pathlib import Path
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from virtualization.bootstrap.create_guest_disks import main, prepare, preflight, verify_base, SIZES

if __name__ == '__main__':
    main()
