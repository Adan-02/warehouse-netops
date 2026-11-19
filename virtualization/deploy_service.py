"""Compatibility entrance. Use python -m virtualization.bootstrap.stage_service_application."""
import sys
from pathlib import Path
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from virtualization.bootstrap.stage_service_application import main

if __name__ == '__main__':
    main()
