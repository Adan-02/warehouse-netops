"""Generator checks and optional isolated Linux runtime fault-injection tests.

Run locally for generator tests. Runtime tests additionally require a disposable
Docker container and WAREHOUSE_DISPOSABLE_CONTAINER=1; they never use a network.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("guangzhou_peer", Path(__file__).with_name("guangzhou_peer.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
IDENTIFIER = "0123456789abcdef0123456789abcdef"
PRIVATE = base64.b64encode(bytes(range(1, 33))).decode()
PUBLIC = base64.b64encode(bytes(reversed(range(1, 33)))).decode()
OWNER = f"{MODULE.MARKER}:{IDENTIFIER}"


class GeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.private = self.root / "private.key"
        self.public = self.root / "router.pub"
        self.private.write_text(PRIVATE + "\n", encoding="ascii")
        self.public.write_text(PUBLIC + "\n", encoding="ascii")
        self.output = self.root / "bundle"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def generate(self, **kwargs):
        return MODULE.generate_bundle(self.output, self.private, self.public, bundle_id=IDENTIFIER, **kwargs)

    def test_wireguard_configuration_contains_only_scoped_addresses(self):
        paths = self.generate()
        config = paths["wg-warehouse.conf"].read_text()
        self.assertIn("Address = 10.89.0.1/32", config)
        self.assertIn("ListenPort = 51830", config)
        self.assertIn("AllowedIPs = 10.89.0.2/32, 192.168.30.10/32", config)
        self.assertIn("PrivateKey = " + PRIVATE, config)
        self.assertNotIn("0.0.0.0/0", config)
        self.assertNotIn("PostUp", config)
        self.assertNotIn("NAT", config)
        self.assertEqual(paths[".project-marker"].read_text().strip(), OWNER)

    def test_manifest_and_runtime_do_not_contain_private_key(self):
        paths = self.generate()
        for name, path in paths.items():
            if name != "wg-warehouse.conf":
                self.assertNotIn(PRIVATE, path.read_text(), name)
        manifest = json.loads(paths["manifest.json"].read_text())
        self.assertEqual(manifest["wan_interface"], "eth0")
        self.assertEqual(manifest["peer_allowed_ips"], ["10.89.0.2/32", "192.168.30.10/32"])

    def test_bundle_checksums_cover_every_payload(self):
        paths = self.generate()
        for path in paths.values():
            self.assertNotIn(b"\r\n", path.read_bytes(), path.name)
        for line in paths["checksums.txt"].read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256(paths[name].read_bytes()).hexdigest(), digest)
        self.assertEqual(len(paths["checksums.txt"].read_text().splitlines()), len(paths) - 1)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are not Windows ACLs")
    def test_private_directory_and_config_permissions(self):
        paths = self.generate()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths["wg-warehouse.conf"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths["install.sh"].stat().st_mode), 0o700)

    def test_nonempty_output_is_not_overwritten(self):
        self.generate()
        original = (self.output / "wg-warehouse.conf").read_bytes()
        with self.assertRaises(ValueError):
            self.generate()
        self.assertEqual((self.output / "wg-warehouse.conf").read_bytes(), original)

    def test_malformed_keys_are_rejected_before_output_creation(self):
        for key in ("not-base64", base64.b64encode(bytes(32)).decode(), base64.b64encode(bytes(31)).decode()):
            self.private.write_text(key)
            with self.assertRaises(ValueError):
                self.generate()
            self.assertFalse(self.output.exists())

    def test_bad_wan_interface_and_bundle_id_are_rejected(self):
        for interface in ("eth0;touch injected", "lo", "wg-warehouse", "a" * 16, ""):
            with self.assertRaises(ValueError):
                self.generate(wan_interface=interface)
        with self.assertRaises(ValueError):
            MODULE.generate_bundle(self.output, self.private, self.public, bundle_id="invalid")
        self.assertFalse(self.output.exists())

    def test_runtime_has_no_us_access_or_global_forwarding_mutations(self):
        paths = self.generate()
        scripts = paths["install.sh"].read_text() + paths["firewall.sh"].read_text()
        for forbidden in ("ssh ", "sshpass", "curl ", "wget ", "sysctl -w", "iptables-restore", "-F INPUT", "-F FORWARD", "-t nat -", "0.0.0.0/0", "systemctl restart qq", "systemctl stop wg-quick@wg0"):
            self.assertNotIn(forbidden, scripts)
        self.assertIn("! -d 10.89.0.1/32", scripts)
        self.assertIn("--ctstate ESTABLISHED,RELATED", scripts)

    def test_systemd_uses_owned_dropin_reload_propagation_and_reconcile(self):
        paths = self.generate()
        firewall = paths["warehouse-wg-firewall.service"].read_text()
        self.assertIn("PartOf=qq-stack-firewall.service", firewall)
        self.assertIn("ReloadPropagatedFrom=qq-stack-firewall.service", firewall)
        self.assertIn("ExecReload=", firewall)
        self.assertIn("OnUnitActiveSec=15s", paths["warehouse-wg-reconcile.timer"].read_text())
        self.assertIn("/var/lib/warehouse-netops-guangzhou/private/wg-warehouse.conf", paths["warehouse.conf"].read_text())


FAKE_COMMANDS = r'''#!/usr/bin/env python3
import json,os,pathlib,shlex,subprocess,sys
name=pathlib.Path(sys.argv[0]).name
p=pathlib.Path('/tmp/warehouse-gz-test/state.json')
d=json.loads(p.read_text());a=sys.argv[1:]
d['calls'].append([name,*a])
def save():p.write_text(json.dumps(d))
def done(code=0,text=''):
    save()
    if text:print(text)
    raise SystemExit(code)
if name=='iptables':
    t='filter'
    while a and a[0] in ('-w','-t'):
        k=a.pop(0);v=a.pop(0)
        if k=='-t':t=v
    op,chain=a.pop(0),a.pop(0);table=d['tables'][t]
    if op=='-S':
        if chain not in table:done(1)
        lines=[('-P '+chain+' '+d['policies'].get(t,{}).get(chain,'ACCEPT')) if chain in d['policies'].get(t,{}) else '-N '+chain]
        for rule in table[chain]:
            text=shlex.join(['-A',chain,*rule])
            if '--comment' in rule:
                owner=rule[rule.index('--comment')+1]
                text=text.replace('--comment '+owner,'--comment "'+owner+'"')
            lines.append(text)
        done(text='\n'.join(lines))
    if op=='-N':
        if chain in table:done(1)
        table[chain]=[];done()
    if chain not in table:done(1)
    if op=='-F':
        if not chain.startswith('WH-WAREHOUSE-'):done(8)
        table[chain]=[];done()
    if op=='-X':
        if table[chain]:done(1)
        for rules in table.values():
            for rule in rules:
                if '-j' in rule and rule[rule.index('-j')+1]==chain:done(1)
        del table[chain];done()
    if op=='-A':table[chain].append(a);done()
    if op=='-I':
        index=int(a.pop(0))-1 if a and a[0].isdigit() else 0
        table[chain].insert(index,a);done()
    if op=='-C':done(0 if a in table[chain] else 1)
    if op=='-D':
        if a not in table[chain]:done(1)
        table[chain].remove(a);done()
    done(9)
if name=='iptables-save':done(text=json.dumps(d['tables'],sort_keys=True))
if name=='ip':
    if a[:3]==['link','show','dev']:done(0 if a[3] in d['interfaces'] else 1)
    if a[:3]==['-j','-4','route']:done(text=json.dumps(d['routes']))
    if a[:3]==['-j','-4','addr']:done(text=json.dumps(d['addresses']))
    if a[:4]==['-4','route','show','dev']:done(text='project routes')
    done(9)
if name=='ss':
    done(text='UNCONN 0 0 0.0.0.0:51830 0.0.0.0:*' if d.get('port_busy') or 'wg-warehouse' in d['interfaces'] else '')
if name=='wg':
    if a[:2]==['show','wg-warehouse'] and 'wg-warehouse' in d['interfaces']:
        done(text='51830' if a[2]=='listen-port' else 'public-peer-key\t0')
    done(1)
if name=='wg-quick':done(9)
if name=='systemctl':
    op=a.pop(0);quiet='--quiet' in a;a=[x for x in a if not x.startswith('--')]
    if op=='daemon-reload':done()
    if op=='show':
        unit=a[-1]
        fragment=d.get('foreign_fragments',{}).get(unit,'')
        if not fragment and pathlib.Path('/etc/systemd/system/'+unit).exists():fragment='/etc/systemd/system/'+unit
        if not fragment and unit=='wg-quick@wg-warehouse.service':fragment='/usr/lib/systemd/system/wg-quick@.service'
        done(text=fragment)
    if op=='is-active':
        success=all(d['services'].get(x,False) for x in a)
        done(0 if success else 3,'' if quiet else ('active' if success else 'inactive'))
    if op=='is-enabled':done(0 if all(x in d['enabled'] for x in a) else 1)
    if op in ('enable','disable'):
        for unit in a:
            if op=='enable' and unit not in d['enabled']:d['enabled'].append(unit)
            if op=='disable' and unit in d['enabled']:d['enabled'].remove(unit)
        done()
    if op in ('start','stop'):
        for unit in a:
            if unit=='wg-quick@wg-warehouse.service':
                if op=='start' and os.environ.get('TEST_FAIL_WG')=='1':done(1)
                if op=='start':
                    d['interfaces']['wg-warehouse']=True
                    d['routes'] += [{'dst':x,'dev':'wg-warehouse'} for x in ('10.89.0.2/32','192.168.30.10/32') if not any(r['dst']==x for r in d['routes'])]
                    d['addresses'] += [{'ifname':'wg-warehouse','addr_info':[{'local':'10.89.0.1','prefixlen':32}]}] if not any(x['ifname']=='wg-warehouse' for x in d['addresses']) else []
                else:
                    d['interfaces'].pop('wg-warehouse',None)
                    d['routes']=[r for r in d['routes'] if r.get('dev')!='wg-warehouse']
                    d['addresses']=[x for x in d['addresses'] if x['ifname']!='wg-warehouse']
            if unit=='warehouse-wg-firewall.service':
                save()
                result=subprocess.run(['sh','/usr/local/lib/warehouse-netops-guangzhou/firewall.sh','up' if op=='start' else 'down'])
                d=json.loads(p.read_text())
                if result.returncode:done(result.returncode)
            d['services'][unit]=op=='start'
        done()
    done(9)
done(9)
'''


ISOLATED = sys.platform == "linux" and Path("/.dockerenv").exists() and os.environ.get("WAREHOUSE_DISPOSABLE_CONTAINER") == "1"


@unittest.skipUnless(ISOLATED, "Runtime fault injection requires an explicitly disposable Docker container")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        GeneratorTests.setUp(self)
        for path in (Path("/var/lib/warehouse-netops-guangzhou"), Path("/usr/local/lib/warehouse-netops-guangzhou"), Path("/etc/systemd/system/wg-quick@wg-warehouse.service.d"), Path("/run/warehouse-netops-guangzhou.lock"), Path("/tmp/warehouse-gz-test")):
            if path.is_dir():shutil.rmtree(path)
        for name in ("warehouse-wg-firewall.service", "warehouse-wg-reconcile.service", "warehouse-wg-reconcile.timer"):
            (Path("/etc/systemd/system") / name).unlink(missing_ok=True)
        Path("/etc/os-release").write_text('ID=debian\nVERSION_ID="13"\n')
        for name, content in (("/etc/wireguard/wg0.conf", "existing-private-config\n"), ("/etc/ssh/sshd_config", "existing-SSH-config\n")):
            p = Path(name);p.parent.mkdir(parents=True, exist_ok=True);p.write_text(content)
        Path("/etc/systemd/system").mkdir(parents=True, exist_ok=True)
        self.state_dir = Path("/tmp/warehouse-gz-test");self.state_dir.mkdir()
        self.state_file = self.state_dir / "state.json"
        self.original_tables = {
            "filter": {"INPUT": [["-j", "QQ-STACK-IN"]], "QQ-STACK-IN": [["-j", "DROP"]], "FORWARD": [["-j", "DOCKER-USER"]], "DOCKER-USER": [["-j", "RETURN"]]},
            "raw": {"PREROUTING": [["-j", "DOCKER"]], "DOCKER": [["-j", "RETURN"]]},
            "nat": {"PREROUTING": [["-j", "DOCKER"]], "DOCKER": []},
        }
        d = {"tables": self.original_tables, "policies": {"filter": {"INPUT": "ACCEPT", "FORWARD": "DROP"}, "raw": {"PREROUTING": "ACCEPT"}},
             "interfaces": {"eth0": True, "wg0": True}, "addresses": [{"ifname":"eth0","addr_info":[{"local":"10.1.0.10","prefixlen":22}]}],
             "routes": [{"dst":"default","dev":"eth0"},{"dst":"10.1.0.0/22","dev":"eth0"},{"dst":"10.77.0.0/24","dev":"wg0"},{"dst":"10.78.0.0/24","dev":"wg0"}],
             "services": {"qq-stack-firewall.service":True,"wg-quick@wg0.service":True,"ssh.service":True,"docker.service":True}, "enabled": [], "calls": []}
        self.state_file.write_text(json.dumps(d))
        self.bin = self.state_dir / "bin";self.bin.mkdir()
        for name in ("iptables", "iptables-save", "ip", "ss", "wg", "wg-quick", "systemctl"):
            p = self.bin / name;p.write_text(FAKE_COMMANDS);p.chmod(0o700)
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = os.environ | {"WAREHOUSE_TEST_BIN":str(self.bin)}
        self.generate()

    def generate(self, **kwargs):
        return GeneratorTests.generate(self, **kwargs)

    def tearDown(self):
        GeneratorTests.tearDown(self)

    def run_install(self, action, **env):
        return subprocess.run(["sh",str(self.output/"install.sh"),action,"--project-dir",str(self.output)],
                              capture_output=True,text=True,timeout=40,env=self.env|env)

    def run_firewall(self, action):
        return subprocess.run(["sh",str(self.output/"firewall.sh"),action],capture_output=True,text=True,timeout=25,env=self.env)

    def state(self):return json.loads(self.state_file.read_text())
    def save(self, state):self.state_file.write_text(json.dumps(state))

    def test_install_and_targeted_rollback_preserve_existing_stack(self):
        result = self.run_install("install")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        state = self.state()
        self.assertIn("wg-warehouse",state["interfaces"])
        self.assertEqual(state["tables"]["filter"]["FORWARD"],self.original_tables["filter"]["FORWARD"])
        self.assertEqual(state["tables"]["nat"],self.original_tables["nat"])
        self.assertEqual(state["tables"]["filter"]["QQ-STACK-IN"],self.original_tables["filter"]["QQ-STACK-IN"])
        self.assertNotIn(PRIVATE,result.stdout+result.stderr)
        self.assertEqual(stat.S_IMODE(Path("/var/lib/warehouse-netops-guangzhou/private/wg-warehouse.conf").stat().st_mode),0o600)
        result = self.run_install("rollback")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        state=self.state()
        self.assertEqual(state["tables"],self.original_tables)
        self.assertNotIn("wg-warehouse",state["interfaces"])
        for unit in ("qq-stack-firewall.service","wg-quick@wg0.service","ssh.service","docker.service"):
            self.assertTrue(state["services"][unit])
            self.assertFalse(any(c[0]=="systemctl" and c[1] in ("start","stop","restart","reload","disable") and unit in c[2:] for c in state["calls"]))
        self.assertEqual(Path("/etc/wireguard/wg0.conf").read_text(),"existing-private-config\n")

    def test_unknown_chain_is_not_adopted_or_changed(self):
        state=self.state();state["tables"]["filter"]["WH-WAREHOUSE-IN"]=[["-j","ACCEPT"]];self.save(state)
        before=self.state()["tables"]
        result=self.run_install("check")
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.state()["tables"],before)
        self.assertFalse(Path("/var/lib/warehouse-netops-guangzhou").exists())

    def test_check_is_nonmutating_and_explicit_marker_is_required(self):
        before=self.state()
        result=self.run_install("check")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        state=self.state()
        for key in ("tables","interfaces","routes","services","enabled"):
            self.assertEqual(state[key],before[key])
        self.assertFalse(Path("/var/lib/warehouse-netops-guangzhou").exists())
        (self.output/".project-marker").write_text("unknown-project\n")
        result=self.run_install("install")
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(Path("/var/lib/warehouse-netops-guangzhou").exists())

    def test_port_and_route_conflicts_are_rejected(self):
        for kind in ("port","route"):
            state=self.state();state["port_busy"]=kind=="port"
            state["routes"]=[r for r in state["routes"] if r.get("dev")!="foreign"]
            if kind=="route":state["routes"].append({"dst":"10.89.0.0/24","dev":"foreign"})
            self.save(state)
            result=self.run_install("check")
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse(Path("/var/lib/warehouse-netops-guangzhou").exists())

    def test_unknown_interface_and_unit_are_not_overwritten(self):
        state=self.state();state["interfaces"]["wg-warehouse"]=True;self.save(state)
        result=self.run_install("check")
        self.assertNotEqual(result.returncode,0)
        self.assertIn("wg-warehouse",self.state()["interfaces"])
        state=self.state();del state["interfaces"]["wg-warehouse"];self.save(state)
        unit=Path("/etc/systemd/system/warehouse-wg-firewall.service")
        unit.write_text("# unrelated existing unit\n")
        result=self.run_install("install")
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(unit.read_text(),"# unrelated existing unit\n")
        self.assertFalse(Path("/var/lib/warehouse-netops-guangzhou").exists())

    def test_unknown_loaded_or_enabled_wireguard_instance_is_not_adopted(self):
        state=self.state();state["foreign_fragments"]={"warehouse-wg-firewall.service":"/run/systemd/transient/warehouse-wg-firewall.service"};self.save(state)
        result=self.run_install("check")
        self.assertNotEqual(result.returncode,0)
        state=self.state();state["foreign_fragments"]={};state["enabled"]=["wg-quick@wg-warehouse.service"];self.save(state)
        result=self.run_install("install")
        self.assertNotEqual(result.returncode,0)
        self.assertIn("wg-quick@wg-warehouse.service",self.state()["enabled"])
        self.assertFalse(Path("/var/lib/warehouse-netops-guangzhou").exists())

    def test_insecure_upload_and_wrong_distribution_are_rejected(self):
        config=self.output/"wg-warehouse.conf"
        config.chmod(0o644)
        result=self.run_install("check")
        self.assertNotEqual(result.returncode,0)
        config.chmod(0o600)
        Path("/etc/os-release").write_text('ID=debian\nVERSION_ID="12"\n')
        result=self.run_install("install")
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(Path("/var/lib/warehouse-netops-guangzhou").exists())

    def test_already_installed_project_can_be_checked_again(self):
        result=self.run_install("install")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        before=self.state()
        result=self.run_install("check")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        state=self.state()
        for key in ("tables","interfaces","routes","services","enabled"):
            self.assertEqual(state[key],before[key])

    def test_same_owner_different_private_key_bundle_cannot_overwrite_active_project(self):
        result=self.run_install("install")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        existing=Path("/var/lib/warehouse-netops-guangzhou/private/wg-warehouse.conf").read_bytes()
        self.private.write_text(base64.b64encode(bytes(range(2,34))).decode())
        second=self.root/"replacement"
        MODULE.generate_bundle(second,self.private,self.public,bundle_id=IDENTIFIER)
        self.output=second
        result=self.run_install("install")
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(Path("/var/lib/warehouse-netops-guangzhou/private/wg-warehouse.conf").read_bytes(),existing)
        self.assertIn("wg-warehouse",self.state()["interfaces"])

    def test_failed_wg_start_rolls_back_only_project(self):
        result=self.run_install("install",TEST_FAIL_WG="1")
        self.assertNotEqual(result.returncode,0)
        state=self.state()
        self.assertEqual(state["tables"],self.original_tables)
        self.assertNotIn("wg-warehouse",state["interfaces"])
        self.assertTrue(Path("/var/lib/warehouse-netops-guangzhou/.failed").exists())
        self.assertFalse(Path("/var/lib/warehouse-netops-guangzhou/private/wg-warehouse.conf").exists())

    def test_reconcile_restores_hooks_removed_by_existing_firewall(self):
        self.assertEqual(self.run_install("install").returncode,0)
        state=self.state()
        state["tables"]["filter"]["INPUT"]=[["-j","QQ-STACK-IN"]]
        state["tables"]["raw"]["PREROUTING"]=[["-j","DOCKER"]]
        self.save(state)
        result=self.run_firewall("reconcile")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.run_firewall("check").returncode,0)
        self.assertEqual(self.state()["tables"]["filter"]["INPUT"][0][-1],"WH-WAREHOUSE-IN")

    def test_foreign_rule_in_owned_chain_is_preserved_and_only_project_wg_stops(self):
        self.assertEqual(self.run_install("install").returncode,0)
        state=self.state();state["tables"]["filter"]["WH-WAREHOUSE-IN"].append(["-j","ACCEPT"]);self.save(state)
        before=self.state()["tables"]
        result=self.run_firewall("reconcile")
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.state()["tables"],before)
        self.assertNotIn("wg-warehouse",self.state()["interfaces"])
        self.assertTrue(self.state()["services"]["wg-quick@wg0.service"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
