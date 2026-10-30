import socket
import unittest
from unittest import mock

from patchyard import ssh
from tests.helpers import make_config

V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 22))
V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::10", 22, 0, 0))


class AddressFamilyTest(unittest.TestCase):
    """Every connection, including the host-key scan, uses service_account.address_family."""

    def test_keyscan_follows_the_address_family(self):
        for family, flag in (("inet", ["-4"]), ("inet6", ["-6"]), ("any", [])):
            cfg = make_config(service_account__address_family=family)
            cmd = ssh.keyscan(cfg, "web1.example.com")
            self.assertEqual(cmd[0], "ssh-keyscan")
            self.assertEqual(cmd[1:1 + len(flag)], flag, family)
            self.assertEqual(cmd[-1], "web1.example.com")
            self.assertIn(f"AddressFamily={family}", ssh.base(cfg))

    def test_keyscan_defaults_to_ipv4(self):
        # A Windows machine in AD DNS has an IPv6 record too; its firewall admits only our IPv4.
        self.assertIn("-4", ssh.keyscan(make_config(), "web1.example.com"))

    def test_resolves_asks_for_the_configured_family(self):
        def fake(host, port, family, kind):
            records = {socket.AF_INET: [V4], socket.AF_INET6: [V6], socket.AF_UNSPEC: [V6, V4]}[family]
            if not records:
                raise socket.gaierror
            return records
        with mock.patch("socket.getaddrinfo", side_effect=fake):
            self.assertEqual(ssh.resolves("web1.example.com", "inet"), "192.0.2.10")
            self.assertEqual(ssh.resolves("web1.example.com", "inet6"), "2001:db8::10")
            self.assertEqual(ssh.resolves("web1.example.com"), "2001:db8::10")

    def test_resolves_returns_none_without_a_record(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
            self.assertIsNone(ssh.resolves("nothing.example.com", "inet"))


if __name__ == "__main__":
    unittest.main()
