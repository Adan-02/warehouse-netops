"""Run an explicit single-line diagnostic on a local lab COM1 console."""
import argparse
import json
from console import Console


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('role', choices=('router', 'office', 'iot', 'service'))
    parser.add_argument('command')
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args()
    console = Console(args.role)
    try:
        result = console.command(args.command, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False))
        return result['exit_code']
    finally:
        console.close()


if __name__ == '__main__':
    raise SystemExit(main())
