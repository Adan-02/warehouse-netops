"""Read-only serial transport probe; transmits only non-sensitive filler."""
import time
from console import Console


def main(argv=None):
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    console = Console('router')
    try:
        console.send('\x03\r')
        time.sleep(.3)
        console.read()
        for size in (64, 128, 256, 360):
            try:
                result = console.command('echo ' + ('A' * size), timeout=4)
                print(size, result['exit_code'], repr(result['output'][-140:]), flush=True)
            except TimeoutError:
                print(size, 'timeout', flush=True)
                console.send('\x03\r')
                time.sleep(.3)
                print(repr(console.read()[-200:]),flush=True)
    finally:
        console.close()


if __name__ == '__main__':
    main()
