"""Serial recovery tests, including an actual interactive POSIX shell/PTY."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import time
import types
import unittest
from unittest import mock


spec=importlib.util.spec_from_file_location('warehouse_console_under_test',Path(__file__).with_name('console.py'))
console_module=importlib.util.module_from_spec(spec)
if os.name=='nt':
    spec.loader.exec_module(console_module)
else:
    # Only native-pipe construction needs pywin32. Subclasses below use a real
    # PTY or a bounded fake; production send/read/command remain unmodified.
    native={name:types.ModuleType(name) for name in ('win32file','win32pipe','win32event','pywintypes')}
    with mock.patch.dict(sys.modules,native):spec.loader.exec_module(console_module)


class FakeConsole(console_module.Console):
    def __init__(self,output,role='office',dropped_ready=0,suppress_completion=False):
        self.role=role;self.sent=[];self.output=output;self.pending=[]
        self.dropped_ready=dropped_ready;self.suppress_completion=suppress_completion
        self.ready_attempts=0;self.ready_acknowledged=False

    def send(self,value):
        self.sent.append(value)
        if value=='\x03\r':self.pending=['old partial shell input\r\n']
        elif 'WH_READY_' in value:
            self.ready_attempts+=1
            marker=re.search(r'WH_READY_[a-f0-9]+',value).group()
            # Echo is not proof that getty has executed the line.
            self.pending=[value+'\r\n']
            if self.ready_attempts>self.dropped_ready:
                self.pending += ['\r\n'+marker[:8],marker[8:]+':0\r\n']
        elif 'WH_END_' in value:
            if not self.ready_acknowledged:
                raise AssertionError('Caller code sent before shell acknowledgement')
            marker=re.search(r'WH_END_[a-f0-9]+',value).group()
            if self.suppress_completion:
                self.pending=[value+'\r\n']
                return
            wire=value+'\r\n'+self.output+'\r\n'+marker+':1\r\n'
            cut=wire.rfind(marker)+8
            self.pending=[wire[:cut],wire[cut:]]
        else:
            raise AssertionError('Unexpected fake TTY input')

    def read(self):
        chunk=self.pending.pop(0) if self.pending else ''
        if ':0\r\n' in chunk and 'WH_READY_' not in chunk:
            self.ready_acknowledged=True
        return chunk


class FakeClock:
    def __init__(self):self.now=0.0
    def monotonic(self):return self.now
    def sleep(self,seconds):self.now+=seconds


class ConsoleProtocolTests(unittest.TestCase):
    def test_stale_input_is_cancelled_and_echo_is_not_an_exit_result(self):
        console=FakeConsole('actual failed command output')
        with mock.patch.object(console_module.time,'sleep'):
            result=console.command('false',timeout=1)
        self.assertEqual(console.sent[0],'\x03\r')
        self.assertEqual(result['exit_code'],1)
        self.assertIn('actual failed command output',result['output'])
        self.assertNotIn('old partial shell input',result['output'])

    def test_invalid_multiline_or_long_command_does_not_cancel_live_input(self):
        console=FakeConsole('',role='router')
        for value in ('echo one\necho two','echo\rmore','x'*381):
            with self.assertRaises(ValueError):console.command(value)
        self.assertEqual(console.sent,[])

    def test_timeout_remains_explicit_when_no_marker_arrives(self):
        class SilentConsole(FakeConsole):
            def read(self):return ''
        console=SilentConsole('')
        with mock.patch.object(console_module.time,'sleep'),self.assertRaises(TimeoutError):
            console.command('true',timeout=0)

    def test_boot_activation_retries_only_readiness_before_caller_code(self):
        console=FakeConsole('completed',role='service',dropped_ready=1)
        clock=FakeClock()
        with mock.patch.object(console_module.time,'monotonic',clock.monotonic), \
                mock.patch.object(console_module.time,'sleep',clock.sleep):
            result=console.command('record-one-operation',timeout=8)
        self.assertEqual(console.ready_attempts,2)
        self.assertEqual(sum('WH_END_' in value for value in console.sent),1)
        self.assertEqual(result['exit_code'],1)
        self.assertNotIn('WH_READY_',result['output'])
        self.assertLess(clock.now,8)

    def test_readiness_failure_is_bounded_and_never_sends_caller_code(self):
        console=FakeConsole('',role='router',dropped_ready=99)
        clock=FakeClock()
        with mock.patch.object(console_module.time,'monotonic',clock.monotonic), \
                mock.patch.object(console_module.time,'sleep',clock.sleep), \
                self.assertRaisesRegex(TimeoutError,'router console shell readiness') as error:
            console.command('private-caller-argument',timeout=8)
        self.assertEqual(console.ready_attempts,3)
        self.assertFalse(any('WH_END_' in value for value in console.sent))
        self.assertNotIn('private-caller-argument',str(error.exception))
        self.assertLess(clock.now,8)

    def test_lost_completion_never_replays_an_acknowledged_caller_command(self):
        console=FakeConsole('',role='iot',suppress_completion=True)
        clock=FakeClock()
        with mock.patch.object(console_module.time,'monotonic',clock.monotonic), \
                mock.patch.object(console_module.time,'sleep',clock.sleep), \
                self.assertRaisesRegex(TimeoutError,'iot console command completion'):
            console.command('one-side-effect',timeout=2)
        self.assertEqual(console.ready_attempts,1)
        self.assertEqual(sum('WH_END_' in value for value in console.sent),1)


@unittest.skipUnless(os.name=='posix','A real POSIX PTY is exercised in the Linux container/CI')
class InteractiveShellRecoveryTests(unittest.TestCase):
    def setUp(self):
        import fcntl
        import pty
        self.pid,self.fd=pty.fork()
        if self.pid==0:
            os.execve('/bin/sh',['sh','-i'],dict(os.environ,PS1='WH_READY> ',PS2='WH_CONT> ',ENV=''))
        flags=fcntl.fcntl(self.fd,fcntl.F_GETFL)
        fcntl.fcntl(self.fd,fcntl.F_SETFL,flags|os.O_NONBLOCK)
        owner=self
        class PtyConsole(console_module.Console):
            def __init__(self):self.role='office'
            def send(self,value):os.write(owner.fd,value.encode())
            def read(self):
                try:return os.read(owner.fd,65536).decode(errors='replace')
                except BlockingIOError:return ''
        self.console=PtyConsole()
        self.wait_for('WH_READY> ')

    def tearDown(self):
        import signal
        try:os.kill(self.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        os.waitpid(self.pid,0)
        os.close(self.fd)

    def wait_for(self,needle):
        output='';deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            output+=self.console.read()
            if needle in output:return output
            time.sleep(.01)
        self.fail('Interactive shell did not reach expected prompt: '+repr(output))

    def test_actual_incomplete_quote_is_cancelled_before_marker_command(self):
        # Establish the same state produced by a truncated boot-time script.
        self.console.send("printf 'unfinished\r")
        self.wait_for('WH_CONT> ')
        result=self.console.command('printf recovered-from-quote',timeout=3)
        self.assertEqual(result['exit_code'],0)
        self.assertRegex(result['output'],r'(?:\r?\n)recovered-from-quote')
        # A second independent command still gets its own accurate exit code.
        failed=self.console.command('false',timeout=3)
        self.assertEqual(failed['exit_code'],1)


if __name__=='__main__':unittest.main()
