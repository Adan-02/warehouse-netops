"""Read-only diagnostics for the independently owned Guangzhou tunnel."""
import json
from cloud_connection import connect_guangzhou
from provision_guests import exec_collect


if __name__ == '__main__':
    client = connect_guangzhou()
    try:
        command = ('sudo -n /usr/sbin/iptables -nvx -L WH-WAREHOUSE-IN; '
                   'sudo -n wg show wg-warehouse latest-handshakes; '
                   'sudo -n wg show wg-warehouse transfer; '
                   'sudo -n ss -uln; '
                   'command -v tcpdump; '
                   'curl --noproxy "*" -fsS --max-time 3 http://metadata.tencentyun.com/latest/meta-data/public-ipv4')
        code, output = exec_collect(client,command,timeout=20)
        print(json.dumps({'exit_code':code,'output':output}))
    finally:
        client.close()
