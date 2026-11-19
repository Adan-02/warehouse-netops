"""Provision staging guests over SSH; a temporary loopback-only reverse proxy
uses the host's already-running HTTP proxy without changing its configuration.
It is closed when provisioning ends and is never part of the final topology.
"""
import argparse
import base64
import codecs
import concurrent.futures
import json
import pathlib
import re
import select
import socket
import threading
import time
import paramiko
from virtualization.console import Console
from virtualization.runtime import PRIVATE

from virtualization.lab_topology import STAGING_ADDRESSES as ADDRESSES


from virtualization.ssh_stream import exec_collect


def connect(role):
    """Pin the guest host key using its non-network local serial console."""
    from virtualization.bootstrap.guards import require_staging
    require_staging()
    console = Console(role)
    try:
        result = console.command("cat /etc/ssh/ssh_host_ed25519_key.pub", timeout=8)
    finally:
        console.close()
    found = re.search(r"ssh-ed25519 ([A-Za-z0-9+/=]+)", result["output"])
    if not found or result["exit_code"]:
        raise RuntimeError(f"{role}: guest SSH host key not yet available via local console")
    client = paramiko.SSHClient()
    client.get_host_keys().add(ADDRESSES[role], "ssh-ed25519",
        paramiko.Ed25519Key(data=base64.b64decode(found.group(1))))
    try:
        client.connect(ADDRESSES[role], username="warehouse", key_filename=str(PRIVATE / "lab_ed25519"),
                       look_for_keys=False, allow_agent=False, timeout=6, auth_timeout=6, banner_timeout=6)
    except Exception:
        client.close()
        raise
    return client


def relay(channel, proxy_port):
    upstream = None
    try:
        upstream = socket.create_connection(("127.0.0.1", proxy_port), timeout=15)
        while True:
            readable, _, _ = select.select([channel, upstream], [], [], 30)
            if not readable:
                continue
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is channel else channel).sendall(data)
    except (OSError, EOFError):
        pass
    finally:
        channel.close()
        if upstream:
            upstream.close()


def provision(role, proxy_port):
    from virtualization.bootstrap.guards import require_staging
    require_staging()
    client = None
    for attempt in range(18):
        try:
            client = connect(role)
            break
        except (OSError, paramiko.SSHException, TimeoutError, RuntimeError) as error:
            if attempt % 3 == 0:
                print(f"{role}: waiting for guest console/SSH ({type(error).__name__})", flush=True)
            time.sleep(3)
    if client is None:
        raise RuntimeError(f"{role}: SSH not ready; leaving VM unchanged")
    transport = client.get_transport()
    remote_port = None
    try:
        remote_port = transport.request_port_forward("127.0.0.1", 18080,
            handler=lambda ch, origin, server: threading.Thread(target=relay, args=(ch, proxy_port), daemon=True).start())
        print(f"{role}: SSH host key pinned; temporary guest-loopback proxy established", flush=True)
        command = (f"sudo env http_proxy=http://127.0.0.1:{remote_port} "
                   f"https_proxy=http://127.0.0.1:{remote_port} "
                   "/usr/local/sbin/warehouse-bootstrap")
        def progress(line):
            stripped = line.rstrip()
            if stripped.startswith(("Get:", "Setting up", "Processing triggers", "WAREHOUSE")):
                print(f"{role}: {stripped}", flush=True)
        # APT output contains no lab credentials. Drain stderr concurrently and
        # retain a bounded failure tail rather than buffering package logs.
        code, output = exec_collect(client, command, timeout=1200, on_line=progress)
        if code:
            print(f"{role}: bootstrap failed exit={code}; {output[-1600:]}", flush=True)
            raise RuntimeError(f"{role}: bootstrap failed")
        status_code, status = exec_collect(client, "cat /var/lib/warehouse/bootstrap.json")
        if status_code:
            raise RuntimeError(f'{role}: readiness marker could not be read')
        record = json.loads(status)
        if record.get('status') != 'ready':
            raise RuntimeError(f'{role}: readiness marker is not ready')
        print(f"{role}: {json.dumps(record)}", flush=True)
        # A manually repaired bootstrap does not erase its initial cloud-init
        # failure. Record that history separately; never rewrite cloud-init data.
    finally:
        try:
            if remote_port is not None:
                transport.cancel_port_forward("127.0.0.1", remote_port)
        finally:
            client.close()


def main(argv=None):
    from virtualization.bootstrap.guards import require_staging
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roles", nargs="+", choices=ADDRESSES)
    parser.add_argument("--host-proxy-port", type=int, default=10808)
    args = parser.parse_args(argv)
    if not 1 <= args.host_proxy_port <= 65535:parser.error("Invalid host proxy port")
    require_staging()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(args.roles)) as pool:
        futures = [pool.submit(provision, role, args.host_proxy_port) for role in args.roles]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()
