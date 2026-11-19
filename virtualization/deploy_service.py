"""Load the already-tested application into fresh lab guests, preserving host data."""
import json
from pathlib import Path
import tarfile
from runtime import IMAGES, PROJECT
from provision_guests import connect


def execute(client, command, timeout=120):
    _, out, err = client.exec_command(command, timeout=timeout)
    out.channel.set_combine_stderr(True)
    data = out.read().decode('utf-8', errors='replace')
    code = out.channel.recv_exit_status()
    if code:
        raise RuntimeError(f'Guest deployment command failed ({code}): {data[-1200:]}')
    return data


def upload_text(client, target, text, mode=0o644):
    with client.open_sftp() as sftp:
        with sftp.open(target, 'w') as f:
            f.write(text)
        sftp.chmod(target, mode)


def main():
    archive = IMAGES / 'warehouse-source.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for directory in ('server', 'tools', 'virtualization/systemd'):
            for path in (PROJECT / directory).rglob('*'):
                if not path.is_file() or path.name == '.env' or '__pycache__' in path.parts or 'runtime' in path.parts:
                    continue
                tar.add(path, arcname=path.relative_to(PROJECT).as_posix(), recursive=False)
    service = connect('service')
    try:
        print('Preparing fresh service guest; host containers and volumes remain in place', flush=True)
        execute(service, 'sudo install -d -o warehouse -g warehouse /opt/warehouse-netops')
        with service.open_sftp() as sftp:
            sftp.put(str(archive), '/home/warehouse/warehouse-source.tar.gz')
            last = [0]
            def progress(done, total):
                if done - last[0] >= 64*1024*1024:
                    last[0] = done
                    print(f'Application images uploaded: {done//(1024*1024)} MiB', flush=True)
            sftp.put(str(IMAGES / 'warehouse-services.tar'), '/home/warehouse/warehouse-services.tar', callback=progress)
        print(execute(service, 'tar -xzf /home/warehouse/warehouse-source.tar.gz -C /opt/warehouse-netops; sudo docker load -i /home/warehouse/warehouse-services.tar', timeout=300), flush=True)
        print(execute(service, 'cd /opt/warehouse-netops/server && python3 setup.py --bind-ip 192.168.200.60 --demo && sudo docker compose --project-name warehouse-netops up -d --no-build --pull never --wait --wait-timeout 120', timeout=180), flush=True)
        print(execute(service, 'curl --noproxy "*" -fsS http://192.168.200.60:8080/healthz'), flush=True)
        execute(service, 'sudo install -m 0644 /opt/warehouse-netops/virtualization/systemd/warehouse-stack.service /etc/systemd/system/warehouse-stack.service; sudo systemctl daemon-reload; sudo systemctl enable warehouse-stack.service')
        with service.open_sftp() as sftp:
            with sftp.open('/opt/warehouse-netops/server/.env') as f:
                environment = f.read().decode()
        password = next(line.split('=',1)[1] for line in environment.splitlines() if line.startswith('MQTT_DEVICE_PASSWORD='))
    finally:
        service.close()
    iot = connect('iot')
    try:
        execute(iot, 'sudo install -d -m 0755 -o warehouse -g warehouse /opt/warehouse-netops/tools; sudo install -d -m 0750 -o warehouse -g warehouse /etc/warehouse')
        upload_text(iot, '/opt/warehouse-netops/tools/publish_sensor.py', (PROJECT/'tools/publish_sensor.py').read_text(encoding='utf-8'))
        upload_text(iot, '/etc/warehouse/device.env', 'MQTT_DEVICE_PASSWORD='+password+'\n', 0o600)
        upload_text(iot, '/home/warehouse/warehouse-sensor-sim.service', (PROJECT/'virtualization/systemd/warehouse-sensor-sim.service').read_text())
        execute(iot, 'sudo install -m 0644 /home/warehouse/warehouse-sensor-sim.service /etc/systemd/system/warehouse-sensor-sim.service; sudo systemctl daemon-reload; sudo systemctl enable warehouse-sensor-sim.service')
        print('IoT publisher and private device credential installed; it will start only after the final-network reboot', flush=True)
    finally:
        iot.close()


if __name__ == '__main__':
    main()
