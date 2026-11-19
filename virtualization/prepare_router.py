"""Compatibility entrance. Use python -m virtualization.bootstrap.create_router."""
import sys
from pathlib import Path
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from virtualization.bootstrap.create_router import main

if __name__ == '__main__':
    main()
