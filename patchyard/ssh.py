"""SSH command lines for the service account, built in one place from the config.

The service account's ~/.ssh/config (written at install) points at the state
directory's ssh/known_hosts and ssh/config, so Ansible and these helpers agree on
host keys and address mappings.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess

from .config import Config

FAMILY = {"inet": "inet", "inet6": "inet6", "any": "any"}
# ssh-keyscan reads no ssh_config, so the address family goes on its command line.
KEYSCAN_FAMILY = {"inet": ["-4"], "inet6": ["-6"], "any": []}
SOCKET_FAMILY = {"inet": socket.AF_INET, "inet6": socket.AF_INET6, "any": socket.AF_UNSPEC}
FAMILY_NAME = {"inet": "IPv4", "inet6": "IPv6", "any": "IPv4 or IPv6"}


def base(cfg: Config, *, batch: bool = True) -> list[str]:
    opts = ["ssh", "-T", "-o", f"AddressFamily={FAMILY[cfg.service_account.address_family]}",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30"]
    if batch:
        opts += ["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", str(cfg.ssh_key)]
    return opts


def as_service(cfg: Config, host: str, remote: str) -> list[str]:
    return [*base(cfg), "-l", cfg.service_account.name, host, remote]


def run_as_service(cfg: Config, host: str, remote: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(as_service(cfg, host, remote), capture_output=True, text=True, timeout=timeout)


def controller_ipv4() -> list[str]:
    """This machine's non-loopback IPv4 addresses (for authorized_keys from=)."""
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True)
    addrs = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if "inet" in parts:
            ip = parts[parts.index("inet") + 1].split("/")[0]
            try:
                if not ipaddress.ip_address(ip).is_loopback:
                    addrs.append(ip)
            except ValueError:
                continue
    return sorted(set(addrs))


def allowed_sources(cfg: Config) -> list[str]:
    return cfg.service_account.allowed_sources or controller_ipv4()


def keyscan(cfg: Config, host: str) -> list[str]:
    """Host-key scan over the same address family as every other connection.

    Without it, a name with both records (Windows registers its IPv6 address in AD DNS)
    is scanned over IPv6, which the machine's firewall and from= admit no one on.
    """
    return ["ssh-keyscan", *KEYSCAN_FAMILY[cfg.service_account.address_family],
            "-T", "8", "-t", "ed25519,ecdsa,rsa", host]


def resolves(host: str, family: str = "any") -> str | None:
    try:
        return socket.getaddrinfo(host, 22, SOCKET_FAMILY[family], socket.SOCK_STREAM)[0][4][0]
    except (OSError, IndexError):
        return None
