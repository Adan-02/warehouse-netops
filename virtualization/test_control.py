"""Offline VM control regressions; shell tests replace poweroff with a function."""
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


# Control tests must import without loading COM1/pywin32 or machine configuration.
console_module = types.ModuleType('console')
console_module.Console = mock.Mock()
runtime_module = types.ModuleType('runtime')
runtime_module.VMRUN = Path('unused-vmrun')
runtime_module.VMS = Path('unused-vms')
runtime_module.vmrun = mock.Mock()
spec = importlib.util.spec_from_file_location('_control_under_test', Path(__file__).with_name('control.py'))
control = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, {'console': console_module, 'runtime': runtime_module}):
    spec.loader.exec_module(control)


def stop_guest(role='router', exit_code=0, command_error=None):
    """Exercise main while every inventory/console/VM operation stays mocked."""
    console = mock.Mock()
    console.command.return_value = {'exit_code': exit_code, 'output': ''}
    console.command.side_effect = command_error
    inventories = [set() if item != role else {role} for item in reversed(control.ROLES)]
    inventories.extend([set(), set()])
    with mock.patch.object(sys, 'argv', ['control.py', 'stop']), \
            mock.patch.object(control, 'running', side_effect=inventories), \
            mock.patch.object(control, 'is_running', side_effect=lambda item, active: item in active), \
            mock.patch.object(control, 'Console', return_value=console), \
            mock.patch.object(control, 'vmrun') as vmrun, \
            redirect_stdout(io.StringIO()):
        try:
            control.main()
        finally:
            vmrun.assert_not_called()
            console.close.assert_called_once_with()
    return console.command.call_args.args[0]


class StopTests(unittest.TestCase):
    def test_start_lock_failure_explains_recovery_without_retry_or_other_mutation(self):
        error = subprocess.CalledProcessError(1, ['fake-vmrun'], output='Error: 文件正在使用中')
        with mock.patch.object(control, 'vmrun', side_effect=error) as vmrun, \
                self.assertRaisesRegex(RuntimeError, 'close its powered-off VMware tab and retry'):
            control.start_guest('router')
        vmrun.assert_called_once_with('start','router','nogui')

    def test_start_other_error_keeps_bounded_diagnostic(self):
        error = subprocess.CalledProcessError(9, ['fake-vmrun'], stderr='VMware diagnostic')
        with mock.patch.object(control, 'vmrun', side_effect=error), \
                self.assertRaisesRegex(RuntimeError, 'exit 9.*VMware diagnostic'):
            control.start_guest('service')

    def test_invalid_budget_is_rejected_before_any_vm_or_console_action(self):
        for budget in ('0', '-1'):
            with self.subTest(budget=budget), \
                    mock.patch.object(sys, 'argv', ['control.py', 'start', '--wait', '--timeout', budget]), \
                    mock.patch.object(control, 'running') as running, \
                    mock.patch.object(control, 'vmrun') as vmrun, \
                    mock.patch.object(control, 'Console') as console, \
                    redirect_stdout(io.StringIO()), \
                    mock.patch('sys.stderr', new_callable=io.StringIO), \
                    self.assertRaises(SystemExit) as error:
                control.main()
            self.assertEqual(error.exception.code, 2)
            running.assert_not_called();vmrun.assert_not_called();console.assert_not_called()

    def test_wait_with_stop_is_rejected_before_shutting_down(self):
        with mock.patch.object(sys, 'argv', ['control.py', 'stop', '--wait']), \
                mock.patch.object(control, 'running') as running, \
                mock.patch.object(control, 'vmrun') as vmrun, \
                mock.patch.object(control, 'Console') as console, \
                mock.patch('sys.stderr', new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as error:
            control.main()
        self.assertEqual(error.exception.code, 2)
        running.assert_not_called();vmrun.assert_not_called();console.assert_not_called()

    def test_each_role_uses_graceful_command(self):
        for role in control.ROLES:
            with self.subTest(role=role):
                command = stop_guest(role)
                self.assertIn('poweroff', command)
                if role != 'router':
                    self.assertEqual(command, 'sudo -n systemctl poweroff --no-block')

    def test_failed_guest_command_is_reported_and_never_force_stops(self):
        for role in control.ROLES:
            with self.subTest(role=role), self.assertRaisesRegex(RuntimeError, role+': graceful shutdown command failed \\(exit code 7\\)'):
                stop_guest(role, exit_code=7)

    def test_console_failure_still_closes_and_never_force_stops(self):
        with self.assertRaisesRegex(TimeoutError, 'fake console timeout'):
            stop_guest(command_error=TimeoutError('fake console timeout'))

    def test_graceful_timeout_never_force_stops(self):
        console = mock.Mock()
        console.command.return_value = {'exit_code': 0, 'output': ''}
        with mock.patch.object(sys, 'argv', ['control.py', 'stop']), \
                mock.patch.object(control, 'running', return_value={'router'}), \
                mock.patch.object(control, 'is_running', side_effect=lambda role, active: role in active), \
                mock.patch.object(control, 'Console', return_value=console), \
                mock.patch.object(control, 'vmrun') as vmrun, \
                mock.patch.object(control.time, 'monotonic', side_effect=[0, 121]), \
                self.assertRaisesRegex(RuntimeError, 'router: graceful shutdown timed out; no forced power-off was attempted'):
            control.main()
        console.close.assert_called_once_with()
        vmrun.assert_not_called()


@unittest.skipUnless(shutil.which('sh'), 'A local POSIX sh is required for console syntax regression')
class RouterShellTests(unittest.TestCase):
    def test_router_command_with_console_suffix_is_valid_shell(self):
        command = stop_guest()
        # Match Console.command's suffix, including its semicolon and "$?".
        script = command + "; printf '\\nWH_END_control:%s\\n' \"$?\""
        result = subprocess.run([shutil.which('sh'), '-n', '-c', script], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_console_marker_precedes_asynchronous_fake_poweroff(self):
        command = stop_guest()
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / 'events.txt'
            release = Path(directory) / 'release'
            environment = dict(os.environ, CONTROL_EVENTS=events.as_posix(), CONTROL_RELEASE=release.as_posix())
            # Hold the delay at a test-controlled barrier. Both functions are
            # inherited by the subshell; no real poweroff can be executed.
            setup = (
                "sleep() { while [ ! -f \"$CONTROL_RELEASE\" ]; do command sleep 0.05; done; }; "
                "poweroff() { printf 'poweroff\\n' >> \"$CONTROL_EVENTS\"; }; "
            )
            suffix = "; printf '\\nWH_END_control:%s\\n' \"$?\"; printf 'marker\\n' >> \"$CONTROL_EVENTS\""
            try:
                result = subprocess.run([shutil.which('sh'), '-c', setup+command+suffix],
                                        env=environment, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('\nWH_END_control:0\n', result.stdout)
                self.assertEqual(events.read_text(), 'marker\n')
            finally:
                release.touch()
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    if events.exists() and 'poweroff\n' in events.read_text():
                        break
                    time.sleep(0.05)
            self.assertEqual(events.read_text(), 'marker\npoweroff\n')


if __name__ == '__main__':
    unittest.main()
