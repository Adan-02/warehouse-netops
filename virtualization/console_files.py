"""Upload small lab configuration files over the existing private COM1 pipe."""
import base64
import hashlib
import shlex
import uuid


def upload(console, destination, data, mode=0o600):
    if not destination.startswith(('/root/warehouse-', '/run/warehouse-')):
        raise ValueError('Only project-owned router paths are accepted')
    existing = console.command('sha256sum ' + shlex.quote(destination) + ' 2>/dev/null')
    if existing['exit_code'] == 0 and hashlib.sha256(data).hexdigest() in existing['output']:
        return
    temporary = '/tmp/wh-up-' + uuid.uuid4().hex[:12]
    quoted = shlex.quote(temporary)
    # Disable shell history; disable terminal echo where stty is available.
    result = console.command('HISTFILE=/dev/null; umask 077; if command -v stty >/dev/null; then stty -echo; fi')
    if result['exit_code']:
        raise RuntimeError('Could not secure console upload')
    try:
        encoded = base64.b64encode(data).decode('ascii')
        # BusyBox ash's interactive editor truncates long serial input lines.
        for index in range(0, len(encoded), 160):
            redirect = '>' if index == 0 else '>>'
            result = console.command('printf %s ' + shlex.quote(encoded[index:index+160]) + ' ' + redirect + ' ' + quoted)
            if result['exit_code']:
                raise RuntimeError('Console upload chunk failed')
        command = ('base64 -d ' + quoted + ' > ' + quoted + '.decoded && chmod ' + oct(mode)[2:] + ' ' + quoted + '.decoded && mv ' + quoted + '.decoded ' + shlex.quote(destination) + ' && rm ' + quoted)
        if console.command(command)['exit_code']:
            raise RuntimeError('Console upload finalization failed')
        check = console.command('sha256sum ' + shlex.quote(destination))
        if check['exit_code'] or hashlib.sha256(data).hexdigest() not in check['output']:
            raise RuntimeError('Console upload checksum mismatch')
    finally:
        console.command('if command -v stty >/dev/null; then stty echo; fi')
