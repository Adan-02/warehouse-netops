"""Prepare the local OpenWrt guest without modifying VMware NAT or cloud hosts."""
import base64
import json
import re
import shlex
import threading
import uuid
import http.server
import urllib.request
import urllib.parse
import paramiko
from console import Console
from runtime import PRIVATE, PROJECT
from provision_guests import exec_collect, relay


class OfficialMirror(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith('/releases/25.12.6/') or '..' in path.split('/'):
            self.send_error(403); return
        try:
            request = urllib.request.Request('https://downloads.openwrt.org' + path,
                headers={'User-Agent': 'warehouse-netops-bootstrap/0.3'})
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read()
            self.send_response(200); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
        except Exception:
            self.send_error(502)

    def log_message(self, *args):
        pass


def write_file(client, path, data):
    code, _ = exec_collect(client, 'umask 077; cat > ' + shlex.quote(path), input_data=data)
    if code:
        raise RuntimeError('Could not write a router bootstrap file')


def backup_feeds(client, paths):
    """Persist every original and a local recovery script before any rewrite."""
    backup = '/root/warehouse-bootstrap-feeds/' + uuid.uuid4().hex
    code, output = exec_collect(client, 'umask 077; mkdir -p /root/warehouse-bootstrap-feeds && '
        'chmod 700 /root/warehouse-bootstrap-feeds && mkdir ' + shlex.quote(backup))
    if code:
        raise RuntimeError('Could not create private feeds backup: ' + output[-500:])
    restore = ['#!/bin/sh', 'set -eu']
    for path in paths:
        if not re.fullmatch(r'/etc/apk/repositories\.d/[A-Za-z0-9_.-]+', path):
            raise RuntimeError('Unexpected repository configuration path')
        saved = backup + '/' + path.rsplit('/', 1)[1]
        code, output = exec_collect(client, 'cp -p ' + shlex.quote(path) + ' ' + shlex.quote(saved) +
            ' && cmp -s ' + shlex.quote(path) + ' ' + shlex.quote(saved))
        if code:
            raise RuntimeError('Could not persist feeds backup at ' + backup + ': ' + output[-500:])
        restore.append('cp -p ' + shlex.quote(saved) + ' ' + shlex.quote(path))
    write_file(client, backup + '/restore.sh', ('\n'.join(restore) + '\n').encode())
    code, _ = exec_collect(client, 'chmod 700 ' + shlex.quote(backup + '/restore.sh'))
    if code:
        raise RuntimeError('Could not finish feeds recovery script at ' + backup)
    return backup


def restore_feeds(client, backup, paths):
    """Attempt all originals even when one fails; keep the persistent backup."""
    failures = []
    for path in paths:
        saved = backup + '/' + path.rsplit('/', 1)[1]
        try:
            code, _ = exec_collect(client, 'cp -p ' + shlex.quote(saved) + ' ' + shlex.quote(path) +
                ' && cmp -s ' + shlex.quote(saved) + ' ' + shlex.quote(path))
            if code:
                failures.append(path)
        except Exception as error:
            failures.append(path + ' (' + type(error).__name__ + ')')
    if failures:
        raise RuntimeError('Feeds restore failed for ' + ', '.join(failures) +
            '; originals and restore.sh remain at ' + backup)


def connect():
    console = Console("router")
    try:
        result = console.command("dropbearkey -y -f /etc/dropbear/dropbear_ed25519_host_key", timeout=8)
    finally:
        console.close()
    match = re.search(r"ssh-ed25519 ([A-Za-z0-9+/=]+)", result["output"])
    if not match:
        raise RuntimeError("Local router host key not available")
    client = paramiko.SSHClient()
    client.get_host_keys().add("192.168.200.50", "ssh-ed25519", paramiko.Ed25519Key(data=base64.b64decode(match.group(1))))
    try:
        client.connect("192.168.200.50", username="root", key_filename=str(PRIVATE / "lab_ed25519"),
                       look_for_keys=False, allow_agent=False, timeout=6, auth_timeout=6, banner_timeout=6)
    except Exception:
        client.close()
        raise
    return client


def main():
    client = connect()
    transport = client.get_transport()
    mirror = http.server.ThreadingHTTPServer(('127.0.0.1', 0), OfficialMirror)
    threading.Thread(target=mirror.serve_forever, daemon=True).start()
    port = None
    feeds_backup = None
    feed_paths = []
    try:
        port = transport.request_port_forward("127.0.0.1", 18080,
            handler=lambda ch, origin, server: threading.Thread(target=relay, args=(ch, mirror.server_port), daemon=True).start())
        src = (PROJECT / "router/virtual-router.sh").read_bytes().replace(b"\r\n", b"\n")
        code, _ = exec_collect(client, "umask 077; cat > /root/virtual-router.sh && chmod 700 /root/virtual-router.sh && sh -n /root/virtual-router.sh", input_data=src)
        if code:
            raise RuntimeError("Router script transfer/syntax failed")
        # uclient-fetch in this image cannot use our HTTP CONNECT staging proxy.
        # Fetch official HTTPS content on the host and deliver it over encrypted
        # SSH loopback forwarding. APK's normal package signatures stay enabled.
        code, paths = exec_collect(client, 'grep -rl downloads.openwrt.org /etc/apk/repositories.d')
        if code or not paths.strip() or len(paths) >= 4096:
            raise RuntimeError('No official feeds found')
        feed_paths = paths.splitlines()
        feeds_backup = backup_feeds(client, feed_paths)
        for path in feed_paths:
            if not re.fullmatch(r'/etc/apk/repositories\.d/[A-Za-z0-9_.-]+', path):
                raise RuntimeError('Unexpected repository configuration path')
            code, content = exec_collect(client, 'cat ' + shlex.quote(feeds_backup + '/' + path.rsplit('/', 1)[1]), tail_limit=65536)
            if code or len(content.encode()) >= 65536:
                raise RuntimeError('Could not safely read a backed-up feed at ' + feeds_backup)
            write_file(client, path, content.replace('https://downloads.openwrt.org', f'http://127.0.0.1:{port}').encode())
        command = "apk update && apk add kmod-wireguard wireguard-tools coreutils-timeout coreutils-base64"
        code, output = exec_collect(client, command, timeout=300,
            on_line=lambda line: print(line, flush=True))
        if code:
            print(output[-1500:], flush=True)
            raise RuntimeError(f"Router package preparation failed ({code})")
        code, output = exec_collect(client, "wg --version && fw4 check && ip -4 route", timeout=20)
        print(output, flush=True)
        if code:
            raise RuntimeError('Router final WireGuard/firewall verification failed')
    finally:
        try:
            if feeds_backup is not None:
                restore_feeds(client, feeds_backup, feed_paths)
        finally:
            try:
                if port is not None:
                    transport.cancel_port_forward("127.0.0.1", port)
            finally:
                client.close()
                mirror.shutdown(); mirror.server_close()


if __name__ == "__main__":
    main()
