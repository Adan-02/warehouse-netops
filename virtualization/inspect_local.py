"""Small read-only COM1 inventory, without any network management adapter."""
import concurrent.futures
import json
from console import Console


def probe(role):
    console = Console(role)
    try:
        command = 'id; ip -4 addr; systemctl is-active warehouse-sensor-sim warehouse-stack'
        return role, console.command(command, timeout=25)
    finally:
        console.close()


def main(argv=None):
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        print(json.dumps(dict(pool.map(probe, ('office', 'iot', 'service'))), indent=2))


if __name__ == '__main__':
    main()
