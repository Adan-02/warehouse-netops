import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from install_observers import router_host_key


class HostPinTests(unittest.TestCase):
    def test_echoed_sed_pattern_is_not_a_second_host_key(self):
        key='ssh-ed25519 '+'A'*68
        output="dropbearkey | sed -n '/^ssh-ed25519 /p'\r\n"+key+' root@OpenWrt\r\n'
        self.assertEqual(router_host_key(output),key)

    def test_echo_only_or_ambiguous_key_is_rejected(self):
        key='ssh-ed25519 '+'A'*68+'\n'
        for value in ("sed -n '/^ssh-ed25519 /p'\r\n",key+key):
            with self.assertRaises(RuntimeError):router_host_key(value)


if __name__=='__main__':unittest.main()
