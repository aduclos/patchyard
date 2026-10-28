"""Enrollment: make a machine manageable, using an admin account's password once.

1. the name resolves and SSH answers
2. host key: recorded on first use, fingerprint logged (compare it if in doubt)
3. bootstrap/bootstrap.sh runs as root via the admin account's sudo: creates the
   local service account (key-only, passwordless sudo, key usable only from the
   controller's addresses)
4. the service account's key login and sudo are verified
5. the machine is added to the managed group in the inventory
6. the auto-update policy is applied, and the dashboard refreshed

The admin password exists only in memory and, briefly, in a 0600 file inside a
0700 tmpfs directory (for SSH_ASKPASS and sudo -S); it is never logged or kept.
"""
from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import IO

from . import inventory, ssh
from .auth import RateLimiter
from .config import Config
from .jobs import JobManager

SUPPORTED = ("debian", "ubuntu")


class Secret:
    """A password as a 0600 file in a 0700 tmpfs dir, removed when the block ends."""

    def __init__(self, password: str):
        self.password = password

    def __enter__(self) -> Secret:
        base = "/dev/shm" if os.path.isdir("/dev/shm") else None
        self.dir = Path(tempfile.mkdtemp(prefix="ansiblefleet-", dir=base))
        self.file = self.dir / "pw"
        fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (self.password + "\n").encode())
        os.close(fd)
        return self

    def askpass(self) -> str:
        helper = self.dir / "askpass"
        helper.write_text(f"#!/bin/sh\nhead -1 {shlex.quote(str(self.file))}\n")
        helper.chmod(0o700)
        return str(helper)

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def normalize_host(cfg: Config, name: str) -> str:
    name = name.strip().lower()
    if name and "." not in name and cfg.site.domain:
        name += cfg.site.domain if cfg.site.domain.startswith(".") else "." + cfg.site.domain
    return name


def known_hosts_file(cfg: Config) -> Path:
    return cfg.state / "ssh" / "known_hosts"


def enroll(cfg: Config, jobs: JobManager, limiter: RateLimiter, log: IO[str],
           host: str, admin: str, password: str) -> int:
    def say(msg: str) -> None:
        log.write(msg + "\n")
        log.flush()

    acct = cfg.service_account.name
    say(f"=== Enrolling {host}: creating {acct} with {admin}'s credentials (used once, not kept)")

    say("--- 1. name and SSH")
    addr = ssh.resolves(host)
    if not addr:
        say("!! the name does not resolve; add it to DNS first")
        return 1
    say(f"resolves to {addr}")

    say("--- 2. host key")
    kh = known_hosts_file(cfg)
    if subprocess.run(["ssh-keygen", "-F", host, "-f", str(kh)], capture_output=True).returncode == 0:
        say("already known (a different key will make SSH refuse; remove the old one if the machine was rebuilt)")
    else:
        scan = subprocess.run(["ssh-keyscan", "-T", "8", "-t", "ed25519,ecdsa,rsa", host],
                              capture_output=True, text=True, timeout=30)
        keys = [line for line in scan.stdout.splitlines() if line and not line.startswith("#")]
        if not keys:
            say("!! could not read the host key; is SSH running?")
            return 1
        fps = subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(keys) + "\n",
                             capture_output=True, text=True).stdout
        for line in fps.splitlines():
            say(f"host key: {line}")
        say("(trusted on first use; compare with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` there)")
        with open(kh, "a") as fh:
            fh.write("\n".join(keys) + "\n")

    say(f"--- 3. create {acct} (as {admin}, via sudo)")
    pub = cfg.ssh_key.with_suffix(".pub").read_text().split()
    sources = ",".join(ssh.allowed_sources(cfg))
    if not sources:
        say("!! no allowed source addresses (set service_account.allowed_sources)")
        return 1
    script = base64.b64encode((cfg.code / "bootstrap" / "bootstrap.sh").read_bytes()).decode()
    env_vars = (f"FLEET_ACCOUNT={shlex.quote(acct)} FLEET_PUBKEY={shlex.quote(pub[0] + ' ' + pub[1] + ' ' + acct)} "
                f"FLEET_ALLOWED_SOURCES={shlex.quote(sources)}")
    remote = (f"B=$(mktemp) && echo {script} | base64 -d > \"$B\" && sudo -S -p '' env {env_vars} sh \"$B\"; "
              "rc=$?; rm -f \"$B\"; exit $rc")
    with Secret(password) as sec:
        env = dict(os.environ, SSH_ASKPASS=sec.askpass(), SSH_ASKPASS_REQUIRE="force", DISPLAY=":0")
        r = subprocess.run([*ssh.base(cfg, batch=False), "-o", "PubkeyAuthentication=no",
                            "-o", "NumberOfPasswordPrompts=1", "-l", admin, host, remote],
                           input=password + "\n", capture_output=True, text=True, timeout=180, env=env)
    for line in (r.stdout + r.stderr).splitlines():
        if line.strip():
            say("  " + line)
    if r.returncode != 0:
        if "Permission denied" in r.stderr:
            limiter.fail()
        if r.returncode == 3:
            say(f"!! the machine's sshd only admits certain users/groups; allow {acct} there "
                f"(e.g. an sshd_config.d drop-in 'AllowGroups {acct}'), then enroll again")
        say(f"!! bootstrap failed (rc {r.returncode})")
        return 1

    say(f"--- 4. {acct} key login and sudo")
    facts = ssh.run_as_service(cfg, host, '. /etc/os-release; echo "$ID|$ID_LIKE|$PRETTY_NAME"; '
                                          'sudo -n true && echo sudo-ok')
    if facts.returncode != 0 or "sudo-ok" not in facts.stdout:
        say("!! key login or sudo as the service account failed: " + (facts.stderr or facts.stdout).strip())
        return 1
    os_id, os_like, pretty = [*facts.stdout.splitlines()[0].split("|"), "", "", ""][:3]
    say(f"{pretty}: key login and passwordless sudo work")
    if os_id not in SUPPORTED and not any(s in os_like for s in SUPPORTED):
        say(f"!! only {', '.join(SUPPORTED)} are supported so far; the account stays, nothing else changed")
        return 1

    say("--- 5. inventory")
    inventory.add(cfg.inventory_file, cfg.inventory.managed_group, host)
    say(f"added {host} to [{cfg.inventory.managed_group}] (backup next to the inventory)")

    say("--- 6. auto-update policy")
    rc = jobs.playbook(log, "auto-updates.yml", [host], [])
    jobs.check(log)
    say(f"=== {host.split('.')[0]} enrolled" + ("" if rc == 0 else
        "; the auto-update policy FAILED (above). Retry with Apply auto-update policy."))
    return rc


def unenroll(cfg: Config, jobs: JobManager, log: IO[str], host: str) -> int:
    inventory.remove(cfg.inventory_file, cfg.inventory.managed_group, host)
    log.write(f"removed {host} from [{cfg.inventory.managed_group}] (backup next to the inventory)\n")
    hv = cfg.state / "host_vars" / f"{host}.yml"
    if hv.exists():
        retired = cfg.state / "host_vars" / "retired"
        retired.mkdir(exist_ok=True)
        dst = retired / f"{hv.name}.{time.strftime('%Y%m%d%H%M')}"
        hv.rename(dst)
        log.write(f"moved its host_vars to {dst}\n")
    acct = cfg.service_account.name
    log.write(f"Nothing on the machine changed: automatic updates keep running and {acct} stays.\n"
              f"Retiring it? There: sudo userdel -r {acct}; sudo rm /etc/sudoers.d/90-{acct}\n")
    jobs.check(log, push=False)
    return 0
