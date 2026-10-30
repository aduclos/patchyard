"""Guard for a public repository: no real addresses or domains in tracked files.

Examples must use documentation values only: IPv4 192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24 (RFC 5737), IPv6 2001:db8::/32 (RFC 3849), and example.com/.net/.org
(RFC 2606). This catches the common way site details leak into a shared code base.
A line may opt out with the marker "site-data: ok" (for version numbers that look like
addresses); keep such exceptions rare and obvious.
"""
import ipaddress
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_V4 = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8",
                                            "0.0.0.0/32")]
DOC_V6 = ipaddress.ip_network("2001:db8::/32")
ALLOWED_DOMAINS = ("example.com", "example.net", "example.org", "github.com", "ntfy.sh", "galaxy.ansible.com",
                   "python.org", "ansible.com", "localhost", "opensource.org", "w3.org",
                   "shields.io", "ko-fi.com", "modelcontextprotocol.io", "simpleicons.org")
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])((?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4})(?![\w:])", re.I)
# TLDs that collide with code (.info, .app, .dev: log.info, cls.app) are left out on purpose.
FQDN = re.compile(r"(?<![\w.@-])((?:[a-z0-9-]+\.)+(?:com|net|org|io|mobi|lan|local|corp|home|internal|biz))\b",
                  re.I)


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    names = out.stdout.split() if out.returncode == 0 else [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")]
    for name in names:
        p = ROOT / name
        if p.is_file() and p.suffix not in {".png", ".ico"} and "collections/" not in name:
            yield p


class NoSiteDataTest(unittest.TestCase):
    def test_only_documentation_addresses_and_domains(self):
        problems = []
        for path in tracked_files():
            if path.name == Path(__file__).name:
                continue
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if "site-data: ok" in line:      # explicit, reviewed exception (e.g. a version number)
                    continue
                for ip in IPV4.findall(line):
                    try:
                        addr = ipaddress.ip_address(ip)
                    except ValueError:
                        continue
                    if not any(addr in net for net in DOC_V4):
                        problems.append(f"{path.relative_to(ROOT)}:{n}: IPv4 {ip}")
                for ip in IPV6.findall(line):
                    try:
                        addr = ipaddress.ip_address(ip)
                    except ValueError:
                        continue
                    if addr.is_global and addr not in DOC_V6:
                        problems.append(f"{path.relative_to(ROOT)}:{n}: IPv6 {ip}")
                for dom in FQDN.findall(line):
                    d = dom.lower()
                    if not any(d == a or d.endswith("." + a) for a in ALLOWED_DOMAINS):
                        problems.append(f"{path.relative_to(ROOT)}:{n}: domain {dom}")
        self.assertEqual(problems, [], "use documentation addresses/domains in examples:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
