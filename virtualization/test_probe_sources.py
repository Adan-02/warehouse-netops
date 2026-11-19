"""Source-program and serial transport regressions, with no VM or cloud access."""
from contextlib import redirect_stdout
import io
import json
import random
import shlex
from types import SimpleNamespace
import unittest
from unittest import mock

try:
    from . import probes
except ImportError:
    import probes


class ProbeSourceTests(unittest.TestCase):
    def execute(self, source, prefix):
        with redirect_stdout(io.StringIO()) as output:
            exec(compile(source, '<remote-probe>', 'exec'), {})
        return probes.extract_json(output.getvalue(), prefix)

    def test_readable_guest_programs_fit_the_shared_serial_limit(self):
        for role in probes.ADDRESSES:
            for purpose in ('inventory', 'probes', 'probe_22', 'probe_1883', 'probe_8080'):
                with self.subTest(role=role, purpose=purpose):
                    source = probes.guest_script(role, purpose, 'WH_TEST_' + 'a' * 32 + '_')
                    compile(source, '<guest-probe>', 'exec')
                    command = probes.encoded_python(source, role=role)
                    self.assertLessEqual(len(command.encode()), 3000)
        with self.assertRaises(ValueError):
            probes.guest_script('office', 'probe_9000', 'WH_TEST_')

    def test_guest_port_probe_binds_selected_source_and_reports_os_error(self):
        connection = mock.Mock()
        connection.getsockname.return_value = ('192.168.20.100', 12345)
        connection.connect.side_effect = ConnectionRefusedError(111, 'private detail')
        with mock.patch('socket.socket', return_value=connection):
            value = self.execute(probes.guest_script('iot', 'probe_1883', 'WH_PORT_'), 'WH_PORT_')
        connection.bind.assert_called_once_with(('192.168.20.100', 0))
        connection.connect.assert_called_once_with(('192.168.30.10', 1883))
        connection.close.assert_called_once()
        self.assertEqual(set(value['ports']), {'1883'})
        self.assertEqual(value['role'], 'iot')
        self.assertEqual(value['purpose'], 'probe_1883')
        self.assertEqual(value['ports']['1883']['source_ip'], '192.168.20.100')
        self.assertEqual(value['ports']['1883']['errno'], 111)
        self.assertFalse(value['ports']['1883']['connected'])
        self.assertNotIn('private detail', json.dumps(value))

    def test_inventory_keeps_completed_evidence_when_later_read_fails(self):
        links = [{'ifname': 'ens33', 'flags': ['UP'],
            'addr_info': [{'family': 'inet', 'local': '192.168.20.100', 'prefixlen': 24}]}]
        with mock.patch('pathlib.Path.exists', return_value=True), \
                mock.patch('subprocess.check_output', side_effect=[json.dumps(links), OSError('private detail')]):
            value = self.execute(probes.guest_script('iot', 'inventory', 'WH_PARTIAL_'), 'WH_PARTIAL_')
        self.assertEqual(value['nics'], [{'name': 'ens33', 'addresses': ['192.168.20.100/24'], 'up': True}])
        self.assertEqual(value['error_type'], 'OSError')
        self.assertNotIn('private detail', json.dumps(value))

    def test_application_health_retains_bounded_response_and_error_shape(self):
        source = (probes.remote_sources('application_health') + '\n'
            + probes.probe_parameters({'service_ip': '192.168.30.10', 'service_port': 8080})
            + 'result=application_health(parameters)\n' + probes.probe_output('WH_HEALTH_'))
        client = mock.Mock()
        response = client.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"state":"healthy"}'
        with mock.patch('http.client.HTTPConnection', return_value=client) as connection:
            value = self.execute(source, 'WH_HEALTH_')
        connection.assert_called_once_with('192.168.30.10', 8080, timeout=4)
        client.request.assert_called_once_with('GET', '/api/health')
        response.read.assert_called_once_with(32768)
        self.assertEqual(value, {'status': 200, 'health': {'state': 'healthy'}})
        client.close.assert_called_once()

    def test_http_handshake_is_optional_and_catches_unavailable_wireguard(self):
        connection = mock.Mock()
        connection.bind.side_effect = OSError('private socket detail')
        prefix = "WH_QUOTE_'\\n_"
        # Prefixes may contain quotes, but must remain data rather than code.
        # A newline is intentionally absent from the actual protocol marker.
        prefix = prefix.replace('\\n', 'literal')
        with mock.patch('socket.socket', return_value=connection), mock.patch('subprocess.run') as run:
            value = self.execute(probes.http_script(prefix, '10.89.0.1'), prefix)
            run.assert_not_called()
        self.assertEqual(value['error_type'], 'OSError')
        self.assertNotIn('private socket detail', json.dumps(value))
        with mock.patch('socket.socket', return_value=connection), mock.patch('subprocess.run',
                return_value=SimpleNamespace(returncode=0, stdout='peer 100\npeer 120\n')) as run, \
                mock.patch('time.time', return_value=125):
            value = self.execute(probes.http_script('WH_WG_', '10.89.0.1', handshake=True), 'WH_WG_')
        self.assertEqual(value['wireguard'], {'command_exit': 0, 'latest_handshake': 120, 'observed_at': 125})
        run.assert_called_once_with(['sudo', '-n', '/usr/bin/wg', 'show', 'wg-warehouse',
            'latest-handshakes'], capture_output=True, text=True, timeout=4)

    def test_plain_and_compressed_transport_round_trip(self):
        for source in ("print('small')", "value='" + 'a' * 5000 + "'\nprint(len(value))"):
            command = probes.encoded_python(source, role='office')
            with redirect_stdout(io.StringIO()) as output:
                exec(shlex.split(command)[2], {})
            self.assertEqual(output.getvalue().strip(), 'small' if 'small' in source else '5000')
        self.assertNotIn('zlib', probes.encoded_python("print('small')"))
        self.assertIn('zlib', probes.encoded_python("print('" + 'a' * 5000 + "')", role='office'))

    def test_uncompressible_command_is_rejected_before_transport(self):
        randomizer = random.Random(0)
        source = ''.join(randomizer.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
            for _ in range(5000))
        with self.assertRaises(probes.ProtocolError):
            probes.encoded_python(source, role='office')


if __name__ == '__main__':
    unittest.main()
