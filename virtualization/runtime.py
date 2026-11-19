"""Local VMware lab artifacts. Never connects to either cloud server."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import re
import shutil
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / "Documents/Codex/warehouse-netops-runtime"
IMAGES = RUNTIME / "images"
VMS = RUNTIME / "vms"
PRIVATE = PROJECT / ".secrets/virtual-lab"
VMRUN = Path("D:/VMware/vmrun.exe")
TOOLS_IMAGE = "warehouse-vm-tools:20261119"
MACS = {
    "router": ["00:50:56:21:10:01", "00:50:56:21:10:02", "00:50:56:21:10:03", "00:50:56:21:10:04"],
    "service": ["00:50:56:21:20:01"],
    "iot": ["00:50:56:21:20:02"],
    "office": ["00:50:56:21:20:03"],
}


def lan_segments() -> dict[str, str]:
    """Read only the three named LAN segments created in VMware's editor."""
    pref = Path(os.environ["APPDATA"]) / "VMware/preferences.ini"
    text = pref.read_text(encoding="utf-8", errors="replace")
    values = dict(re.findall(r'^\s*(pref\.namedPVNs\d+\.(?:name|pvnID))\s*=\s*"([^"\n]+)"', text, re.M))
    found = {}
    for key, name in values.items():
        if key.endswith(".name") and name in ("wh-office", "wh-iot", "wh-service"):
            ident = values.get(key[:-5] + ".pvnID")
            if ident:
                if not re.fullmatch(r'[0-9a-f]{2}(?: [0-9a-f]{2}){7}-(?:[0-9a-f]{2} ){7}[0-9a-f]{2}', ident, re.I):
                    raise RuntimeError('Invalid VMware LAN segment identifier format')
                found[name[3:]] = ident
    if len(found) != 3:
        raise RuntimeError("Create wh-office, wh-iot, wh-service LAN segments in VMware first")
    if len(set(found.values())) != 3:
        raise RuntimeError('The three zones must use three distinct LAN segments')
    return found


def prepare_dirs() -> None:
    for p in (IMAGES, VMS, PRIVATE):
        p.mkdir(parents=True, exist_ok=True)


def digest(path: Path, algorithm: str = "sha256") -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, target: Path, expected: str, algorithm: str = "sha256") -> None:
    if target.exists() and digest(target, algorithm) == expected:
        print(f"Verified cached {target.name}", flush=True)
        return
    partial = target.with_suffix(target.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "warehouse-netops-lab/0.3"})
    with urllib.request.urlopen(request, timeout=45) as response, partial.open("wb") as f:
        done = 0
        while chunk := response.read(1024 * 1024):
            f.write(chunk)
            done += len(chunk)
            if done % (32 * 1024 * 1024) == 0:
                print(f"Downloading {target.name}: {done // (1024*1024)} MiB", flush=True)
    if digest(partial, algorithm) != expected:
        raise ValueError(f"Checksum mismatch for {target.name}; partial file retained")
    partial.replace(target)
    print(f"Downloaded and verified {target.name}", flush=True)


def tool(*args: str, mount: Path = RUNTIME) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "run", "--rm", "--network", "none", "--mount",
                           f"type=bind,source={mount},target=/work", TOOLS_IMAGE, *args],
                          check=True, text=True, encoding="utf-8", capture_output=True)


def ensure_stopped(role: str) -> Path:
    if role not in MACS:
        raise ValueError('Unknown lab role')
    directory = VMS / role
    target = directory / f'{role}.vmx'
    result = subprocess.run([str(VMRUN), 'list'], check=True, text=True,
                            encoding='utf-8', capture_output=True)
    if str(target).casefold() in {s.strip().casefold() for s in result.stdout.splitlines()[1:]}:
        raise RuntimeError(f'{role}: power off the lab VM before changing its configuration')
    if directory.exists() and any(directory.glob('*.lck')):
        raise RuntimeError(f'{role}: VMware still holds a lock; close its settings/tab first')
    return target


def isolate_guest(role: str) -> Path:
    """Change only a powered-off guest's network, retaining its active disk/UUID."""
    if role not in ('service', 'iot', 'office'):
        raise ValueError('Only the three single-NIC guests can be isolated')
    target = ensure_stopped(role)
    original = target.read_text(encoding='utf-8')
    backup = target.with_suffix('.vmx.before-isolation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    shutil.copy2(target, backup)
    network = {'ethernet0.connectionType': 'pvn', 'ethernet0.pvnID': lan_segments()[role],
               'ethernet0.displayName': 'wh-' + role, 'ethernet0.startConnected': 'TRUE'}
    remove = {*network, 'ethernet0.vnet'}
    lines = [s for s in original.splitlines() if s.split('=', 1)[0].strip() not in remove]
    lines.extend(f'{k} = "{v}"' for k, v in network.items())
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return target


def vmx(role: str, disk: Path, *, stage: bool = True, seed: Path | None = None, replace: bool = False) -> Path:
    target = ensure_stopped(role)
    if target.exists() and not replace:
        raise FileExistsError(f'{role}: existing VMX protected; use the explicit offline network-switch function')
    directory = VMS / role
    directory.mkdir(parents=True, exist_ok=True)
    memory = {"router": 512, "service": 2048, "iot": 512, "office": 1536}[role]
    # Conservative profile for this WHP host; leave Hyper-V/WSL unchanged.
    cpus = 1
    settings = {
        ".encoding": "UTF-8", "config.version": "8", "virtualHW.version": "20",
        "displayName": f"Warehouse-{role}", "guestOS": "other5xlinux-64",
        "memsize": str(memory), "numvcpus": str(cpus), "firmware": "bios",
        "ide0:0.present": "TRUE", "ide0:0.fileName": str(disk), "ide0:0.deviceType": "disk",
        "floppy0.present": "FALSE", "usb.present": "FALSE", "sound.present": "FALSE",
        "tools.syncTime": "TRUE", "mks.enable3d": "FALSE", "msg.autoAnswer": "TRUE",
        "uuid.action": "create", "vhv.enable": "FALSE",
    }
    for i, mac in enumerate(MACS[role]):
        prefix = f"ethernet{i}."
        settings.update({prefix + "present": "TRUE", prefix + "virtualDev": "e1000",
                         prefix + "addressType": "static", prefix + "address": mac,
                         prefix + "startConnected": "TRUE"})
        if (role == "router" and i == 0) or (role != "router" and stage):
            settings[prefix + "connectionType"] = "custom"
            settings[prefix + "vnet"] = "VMnet8"
        else:
            segment = {1: "office", 2: "iot", 3: "service"}[i] if role == "router" else role
            settings[prefix + "connectionType"] = "pvn"
            settings[prefix + "pvnID"] = lan_segments()[segment]
            settings[prefix + "displayName"] = "wh-" + segment
    if seed:
        settings.update({"ide1:0.present": "TRUE", "ide1:0.deviceType": "cdrom-image",
                         "ide1:0.fileName": str(seed), "ide1:0.startConnected": "TRUE"})
    settings.update({"serial0.present": "TRUE", "serial0.fileType": "pipe",
                     "serial0.fileName": "\\\\.\\pipe\\warehouse-" + role,
                     "serial0.pipe.endPoint": "server", "serial0.tryNoRxLoss": "FALSE",
                     "serial0.pipe.otherEnd": "app",
                     "serial0.yieldOnMsrRead": "TRUE"})
    # VMX does not use JSON/C backslash escaping (notably for named pipes).
    target.write_text("\n".join(f'{k} = "{v.replace(chr(34), "|22")}"' for k, v in settings.items()) + "\n", encoding="utf-8")
    return target


def vmrun(action: str, role: str, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(VMRUN), "-T", "ws", action, str(VMS / role / f"{role}.vmx"), *arguments],
                          check=True, text=True, encoding="utf-8", capture_output=True)
