"""Fault tests for SSH output draining and feeds recovery; no VM/network I/O."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

# Runtime entry points are also standalone scripts with sibling imports.
# Support both root-level unittest module invocation and direct execution here.
MODULE_DIR = str(Path(__file__).resolve().parent)
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import bootstrap_router as router
import provision_guests as guests


class Channel:
    def __init__(self, chunks, exit_code=0):
        self.chunks = iter(chunks)
        self.combined = False
        self.drained = False
        self.closed = False
        self.exit_code = exit_code

    def set_combine_stderr(self, enabled):
        self.combined = enabled

    def shutdown_write(self):
        pass

    def recv(self, size):
        if not self.combined:
            raise AssertionError('stderr would block the shared SSH window')
        chunk = next(self.chunks, b'')
        if isinstance(chunk, Exception):
            raise chunk
        if not chunk:
            self.drained = True
        return chunk

    def recv_exit_status(self):
        if not self.drained:
            raise AssertionError('exit status awaited before draining output')
        return self.exit_code

    def close(self):
        self.closed = True


def ssh_client(channel):
    client = mock.Mock()
    stdin = mock.Mock(channel=channel)
    client.exec_command.return_value = (stdin, mock.Mock(channel=channel), mock.Mock())
    return client


class TransportTests(unittest.TestCase):
    def test_large_combined_stderr_is_drained_and_tail_is_bounded(self):
        channel = Channel([b'x' * 65536] * 80 + [b'failure detail'], exit_code=7)
        code, output = guests.exec_collect(ssh_client(channel), 'package install', tail_limit=128)
        self.assertEqual(code, 7)
        self.assertEqual(len(output), 128)
        self.assertTrue(output.endswith('failure detail'))
        self.assertTrue(channel.closed)

    def test_utf8_progress_survives_chunk_boundary(self):
        channel = Channel([b'first\n\xe6', b'\xb5\x8b\xe8\xaf\x95\r\nlast'])
        lines = []
        code, output = guests.exec_collect(ssh_client(channel), 'command', on_line=lines.append)
        self.assertEqual((code, output), (0, 'first\n测试\r\nlast'))
        self.assertEqual(lines, ['first', '测试', 'last'])

    def test_receive_failure_closes_channel(self):
        channel = Channel([TimeoutError('stalled remote')])
        with self.assertRaises(TimeoutError):
            guests.exec_collect(ssh_client(channel), 'command')
        self.assertTrue(channel.closed)

    def test_input_is_sent_only_after_stderr_is_combined(self):
        channel = Channel([])
        client = ssh_client(channel)
        client.exec_command.return_value[0].write.side_effect = lambda data: self.assertTrue(channel.combined)
        guests.exec_collect(client, 'cat > file', input_data=b'payload')


class FeedsTests(unittest.TestCase):
    paths = ['/etc/apk/repositories.d/a.list', '/etc/apk/repositories.d/b.list']

    def test_backup_is_private_and_contains_durable_recovery_script(self):
        calls = []
        def execute(client, command, **kwargs):
            calls.append((command, kwargs.get('input_data')))
            return 0, ''
        with mock.patch.object(router, 'exec_collect', side_effect=execute):
            backup = router.backup_feeds(mock.Mock(), self.paths)
        self.assertIn('umask 077', calls[0][0])
        self.assertIn('chmod 700 /root/warehouse-bootstrap-feeds', calls[0][0])
        copies = [command for command, data in calls if command.startswith('cp -p ')]
        self.assertEqual(len(copies), 2)
        self.assertTrue(all('cmp -s' in command for command in copies))
        script = next(data.decode() for command, data in calls if 'cat > ' in command)
        self.assertIn(backup + '/a.list', script)
        self.assertIn(backup + '/b.list', script)
        # Backups precede creation of the recovery script; live feeds are not written.
        self.assertLess(calls.index((copies[-1], None)), next(i for i, item in enumerate(calls) if 'cat > ' in item[0]))
        self.assertFalse(any('cat > /etc/' in command for command, data in calls))

    def test_restore_attempts_remaining_files_after_error(self):
        with mock.patch.object(router, 'exec_collect', side_effect=[TimeoutError(), (1, '')]) as execute:
            with self.assertRaisesRegex(RuntimeError, 'restore.sh remain at /root/saved'):
                router.restore_feeds(mock.Mock(), '/root/saved', self.paths)
        self.assertEqual(execute.call_count, 2)

    def run_failed_bootstrap(self, fail_rewrite=False):
        client = mock.Mock()
        client.get_transport.return_value.request_port_forward.return_value = 18080
        server = mock.Mock(server_port=12345)
        commands = []
        def execute(unused_client, command, **kwargs):
            commands.append(command)
            if command.startswith('grep -rl '):
                return 0, '\n'.join(self.paths) + '\n'
            if command.startswith('cat /root/warehouse-bootstrap-feeds/'):
                return 0, 'https://downloads.openwrt.org/releases/25.12.6/a.apk\n'
            if fail_rewrite and command == 'umask 077; cat > ' + self.paths[1]:
                raise OSError('connection lost during rewrite')
            if command.startswith('apk update'):
                return 9, 'package failure'
            return 0, ''
        with mock.patch.object(router, 'connect', return_value=client), \
             mock.patch.object(router.http.server, 'ThreadingHTTPServer', return_value=server), \
             mock.patch.object(router.threading, 'Thread'), \
             mock.patch.object(router, 'exec_collect', side_effect=execute), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises((RuntimeError, OSError)):
                router.main()
        restores = [command for command in commands if command.startswith('cp -p /root/warehouse-bootstrap-feeds/')]
        self.assertEqual(len(restores), 2)
        self.assertTrue(all('cmp -s' in command for command in restores))
        first_rewrite = next(i for i, command in enumerate(commands) if command.startswith('umask 077; cat > /etc/'))
        recovery = next(i for i, command in enumerate(commands) if command.startswith('chmod 700 /root/warehouse-bootstrap-feeds/') and command.endswith('/restore.sh'))
        self.assertLess(recovery, first_rewrite)
        client.get_transport.return_value.cancel_port_forward.assert_called_once_with('127.0.0.1', 18080)
        client.close.assert_called_once()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()

    def test_package_failure_restores_all_feeds_and_closes_forward(self):
        self.run_failed_bootstrap()

    def test_partial_rewrite_failure_restores_all_feeds_and_closes_forward(self):
        self.run_failed_bootstrap(fail_rewrite=True)


if __name__ == '__main__':
    unittest.main()
