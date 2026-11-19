"""One explicitly exclusive lab operation with only read-only probes.

Scenario modules own fault injection and policy verdicts. This module owns
connection lifetimes, source-bound reads and the whole-operation serial lock.
"""
from __future__ import annotations
import json
import re
import uuid
try:
    from .probes import (ProtocolError,encoded_python,extract_json,guest_script,
        extract_nft,rules_from_nft,rules_fingerprint,http_script,ssh_probe_script)
    from .lab_topology import OFFICE_IP,CLOUD_IP
    from .transport_limits import command_limit
except ImportError:
    from probes import (ProtocolError,encoded_python,extract_json,guest_script,
        extract_nft,rules_from_nft,rules_fingerprint,http_script,ssh_probe_script)
    from lab_topology import OFFICE_IP,CLOUD_IP
    from transport_limits import command_limit


class LabSession:
    def __init__(self, console_factory, *, cloud_factory=None):
        self.console_factory=console_factory
        self.consoles={}
        self.collection_stage='not_started'
        self.cloud_factory=cloud_factory
        self.cloud_client=None
        self._operation_lock=None
        if getattr(console_factory,'uses_lab_operation_lock',False) is True:
            # The compatibility Console constructor also locks for standalone
            # scripts; inside a session the explicit operation lock owns it.
            try:
                from .console import LabOperationLock,VmSerialShell
            except ImportError:
                from console import LabOperationLock,VmSerialShell
            self._operation_lock=LabOperationLock()
            self._operation_lock.acquire()
            self.console_factory=VmSerialShell

    def __enter__(self):return self

    def __exit__(self,*args):self.close()

    def console(self, role):
        if role not in self.consoles:
            self.consoles[role]=self.console_factory(role)
        return self.consoles[role]

    def close(self):
        try:
            for console in self.consoles.values():
                try:console.close()
                except Exception:pass
            self.consoles.clear()
            if self.cloud_client is not None:
                self.cloud_client.close();self.cloud_client=None
        finally:
            if self._operation_lock is not None:
                self._operation_lock.close();self._operation_lock=None

    def guest(self, role, purpose):
        self.collection_stage = role + "_" + purpose
        prefix = "WH_MATRIX_" + uuid.uuid4().hex + "_"
        command = encoded_python(guest_script(role, purpose, prefix),role=role)
        result = self.console(role).command(command, timeout=35)
        if result["exit_code"] != 0:
            raise ProtocolError("Guest Python check failed; inspect the VM locally.")
        data = extract_json(result["output"], prefix)
        if data.get("error_type"):
            raise ProtocolError("Guest inventory or probe dependencies are unavailable.")
        return data


    def router(self):
        self.collection_stage = "router_fw4_nft"
        prefix = "WH_FW4_" + uuid.uuid4().hex
        command = "fw4 check >/dev/null 2>&1; wh_fw_rc=$?; printf '\\n" + prefix + ":%s\\n' \"$wh_fw_rc\"; nft -a -j list table inet fw4"
        result = self.console("router").command(command, timeout=35)
        matches = re.findall(r"(?:^|\r?\n)" + re.escape(prefix) + r":(\d+)(?:\r?\n)", result["output"])
        if result["exit_code"] != 0 or len(matches) != 1:
            raise ProtocolError("Router did not return complete fw4/nft evidence.")
        rules = rules_from_nft(extract_nft(result["output"]))
        return {"fw4_check_exit": int(matches[0]), "fingerprint": rules_fingerprint(rules), "rules": rules}


    def command(self, role, command, timeout=45):
        result = self.console(role).command(command, timeout=timeout)
        if result["exit_code"] != 0:
            # Do not export arbitrary serial output or private file contents.
            raise ProtocolError(f"{role} command failed (exit {result['exit_code']}).")
        return result["output"]


    def local_probe(self):
        prefix = "WH_FAULT_" + uuid.uuid4().hex + "_"
        command = encoded_python(http_script(prefix, OFFICE_IP),role='office')
        return extract_json(self.command("office", command, timeout=15), prefix)


    def cloud_probe(self):
        if self.cloud_factory is None:
            raise ProtocolError("Guangzhou checking was not enabled.")
        if self.cloud_client is None:
            self.cloud_client = self.cloud_factory()
        prefix = "WH_CLOUD_" + uuid.uuid4().hex + "_"
        command = encoded_python(http_script(prefix, CLOUD_IP, handshake=True))
        _, output, _ = self.cloud_client.exec_command(command, timeout=15)
        output.channel.set_combine_stderr(True)
        payload = output.read(16385)
        if len(payload) > 16384 or output.channel.recv_exit_status() != 0:
            raise ProtocolError("Guangzhou read-only HTTP/WireGuard probe failed.")
        return extract_json(payload.decode("utf-8", errors="replace"), prefix)


    def cloud_ssh_probe(self):
        # Called only for broker-stop cloud checks. WAN checks have no
        # dependency on this private key, host pin or service SSH account.
        if self.cloud_factory is None:
            raise ProtocolError('Guangzhou checking was not enabled.')
        if self.cloud_client is None:
            self.cloud_client = self.cloud_factory()
        prefix = 'WH_CLOUD_SSH_' + uuid.uuid4().hex + '_'
        command = 'sudo -n ' + encoded_python(ssh_probe_script(prefix))
        _, output, _ = self.cloud_client.exec_command(command, timeout=20)
        output.channel.set_combine_stderr(True)
        payload = output.read(16385)
        if len(payload) > 16384 or output.channel.recv_exit_status() != 0:
            raise ProtocolError('Guangzhou dedicated-key SSH probe failed.')
        return extract_json(payload.decode('utf-8', errors='replace'), prefix)


    def wan_state(self):
        output = self.command("router", "ubus call network.interface.wan status", timeout=15)
        for start, char in enumerate(output):
            if char != "{":
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(output[start:])
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("up"), bool):
                return {"up": value["up"], "pending": value.get("pending"), "available": value.get("available")}
        raise ProtocolError("Router did not return a complete WAN state.")
