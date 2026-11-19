"""Use the VM's local COM1 named pipe, without adding a management network."""
import json
import time
import uuid
import win32file
import win32pipe
import win32event
import pywintypes


class Console:
    def __init__(self, role):
        self.role = role
        self.pipe = None
        self.mutex = win32event.CreateMutex(None, False, r'Local\WarehouseNetopsCOM1Checks')
        self.locked = False
        try:
            result = win32event.WaitForSingleObject(self.mutex, 0)
            if result == win32event.WAIT_TIMEOUT:
                print('Waiting for another local lab check to release COM1...',flush=True)
                result = win32event.WaitForSingleObject(self.mutex, 90000)
            if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                raise TimeoutError('Another local lab check is still using COM1')
            self.locked = True
            self.pipe = win32file.CreateFile(r"\\.\pipe\warehouse-" + role,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                win32file.OPEN_EXISTING, 0, None)
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.pipe is not None:
                win32file.CloseHandle(self.pipe)
                self.pipe = None
        finally:
            if self.mutex is not None:
                if self.locked:
                    win32event.ReleaseMutex(self.mutex)
                    self.locked = False
                win32file.CloseHandle(self.mutex)
                self.mutex = None

    def read(self):
        available = win32pipe.PeekNamedPipe(self.pipe, 0)[1]
        if not available:
            return ""
        return win32file.ReadFile(self.pipe, min(available, 65536))[1].decode("utf-8", errors="replace")

    def send(self, command):
        # Emulated UART input can overrun the small guest receive FIFO when a
        # whole encoded script is written at once under VMware's WHP backend.
        data = command.encode("utf-8")
        for offset in range(0, len(data), 16):
            win32file.WriteFile(self.pipe, data[offset:offset + 16])
            if len(data) > 16:
                time.sleep(0.01)

    def command(self, command, timeout=30):
        limit = 380 if self.role == 'router' else 3000
        if '\n' in command or '\r' in command or len(command.encode('utf-8')) > limit:
            raise ValueError(f'Use a single {self.role} console command below {limit} bytes; transfer larger scripts in chunks')
        marker = "WH_END_" + uuid.uuid4().hex
        # A previous boot-time UART overrun can leave the interactive shell in
        # an unfinished quote. CR alone extends that command and never reaches
        # our result marker. The mutex owns this lab TTY, so cancel any stale
        # partial input before starting the next independent command.
        self.send("\x03\r")
        time.sleep(0.15)
        self.read()
        self.send(command + "; printf '\\n" + marker + ":%s\\n' \"$?\"\r")
        result = ""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result += self.read()
            # Ignore echoed command, match only a completed result line.
            import re
            match = re.search(r"(?:\r?\n)" + marker + r":(\d+)(?:\r?\n)", result)
            if match:
                return {"exit_code": int(match.group(1)), "output": result[:match.start()]}
            time.sleep(0.1)
        raise TimeoutError("Local VM console command timed out")
