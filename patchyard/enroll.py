"""Enrollment: make a machine manageable, using an admin account's password once.

1. the name resolves and SSH answers
2. host key: recorded on first use, fingerprint logged (compare it if in doubt)
3. the bootstrap (bootstrap/bootstrap.sh, or bootstrap.ps1 on Windows) creates the local
   service account: key login only, usable only from the controller's addresses, with
   passwordless sudo (Linux/BSD) or as a local administrator (Windows). Either Patchyard
   runs it over SSH with an admin account's password, or the person pastes the one-line
   command shown in the UI on the machine (for machines without password logins).
4. the service account's key login and privileges are verified
5. the machine is added to the managed group in the inventory
6. the auto-update policy is applied, and the dashboard refreshed

The admin password exists only in memory and, briefly, in a 0600 file inside a
0700 tmpfs directory (for SSH_ASKPASS and sudo -S); it is never logged or kept.
"""
from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from . import inventory, machines, ssh
from .auth import RateLimiter
from .config import Config
from .jobs import JobManager

SUPPORTED = ("debian", "ubuntu", "fedora", "rhel", "centos", "rocky", "almalinux",
             "opensuse", "suse", "sles", "arch", "alpine", "freebsd", "macos")


class Secret:
    """A password as a 0600 file in a 0700 tmpfs dir, removed when the block ends."""

    def __init__(self, password: str):
        self.password = password

    def __enter__(self) -> Secret:
        base = "/dev/shm" if os.path.isdir("/dev/shm") else None
        self.dir = Path(tempfile.mkdtemp(prefix="patchyard-", dir=base))
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


def bootstrap_values(cfg: Config) -> tuple[str, str, str]:
    """(account, public key line, allowed sources) the bootstrap scripts need."""
    pub = cfg.ssh_key.with_suffix(".pub").read_text().split()
    acct = cfg.service_account.name
    sources = ",".join(ssh.allowed_sources(cfg))
    if not sources:
        raise ValueError("no allowed source addresses (set service_account.allowed_sources)")
    return acct, f"{pub[0]} {pub[1]} {acct}", sources


def linux_command(cfg: Config, *, sudo: str = "sudo") -> str:
    """One line to paste on a Linux/BSD machine (as a user who can sudo, or as root)."""
    acct, pub, sources = bootstrap_values(cfg)
    script = base64.b64encode((cfg.code / "bootstrap" / "bootstrap.sh").read_bytes()).decode()
    env_vars = (f"PATCHYARD_ACCOUNT={shlex.quote(acct)} PATCHYARD_PUBKEY={shlex.quote(pub)} "
                f"PATCHYARD_ALLOWED_SOURCES={shlex.quote(sources)}")
    return (f"B=$(mktemp) && echo {script} | base64 -d > \"$B\" && {sudo} env {env_vars} sh \"$B\"; "
            "rc=$?; rm -f \"$B\"; exit $rc")


# Windows' command-line limit (CreateProcess). The command is PowerShell-only: it is longer
# than Command Prompt's 8191-character line limit.
WINDOWS_CMD_MAX = 32767


def _compact_ps(script: str) -> str:
    """bootstrap.ps1 without comments, indentation or blank lines (it becomes base64 UTF-16, so
    every character counts four times over). Only full-line comments and comments after two or
    more spaces are removed; the script keeps "  #" out of its strings for this reason."""
    out = []
    for line in script.splitlines():
        line = re.sub(r"\s{2,}#\s.*$", "", line).strip()
        if line and not line.startswith("#"):
            out.append(line)
    return "\n".join(out) + "\n"


def windows_command(cfg: Config) -> str:
    """One line to paste into an administrator PowerShell (not Command Prompt: too long for it)."""
    acct, pub, sources = bootstrap_values(cfg)
    ps = (f"$env:PATCHYARD_ACCOUNT='{acct}'; $env:PATCHYARD_PUBKEY='{pub}'; "
          f"$env:PATCHYARD_ALLOWED_SOURCES='{sources}'\n"
          + _compact_ps((cfg.code / "bootstrap" / "bootstrap.ps1").read_text()))
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def _record_host_key(cfg: Config, host: str, say: Callable[[str], None]) -> bool:
    kh = known_hosts_file(cfg)
    if subprocess.run(["ssh-keygen", "-F", host, "-f", str(kh)], capture_output=True).returncode == 0:
        say("already known (a different key will make SSH refuse; remove the old one if the machine was rebuilt)")
        return True
    scan = subprocess.run(ssh.keyscan(cfg, host), capture_output=True, text=True, timeout=30)
    keys = [line for line in scan.stdout.splitlines() if line and not line.startswith("#")]
    if not keys:
        say("!! could not read the host key; is the SSH server running and reachable?")
        return False
    fps = subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(keys) + "\n",
                         capture_output=True, text=True).stdout
    kh.parent.mkdir(parents=True, exist_ok=True)
    for line in fps.splitlines():
        say(f"host key: {line}")
    say("(trusted on first use; compare with the machine's own host key fingerprint if in doubt)")
    with open(kh, "a") as fh:
        fh.write("\n".join(keys) + "\n")
    return True


class PasswordRefused(Exception):
    """The admin password was rejected: a batch stops here rather than lock the account."""


def _run_bootstrap_over_ssh(cfg: Config, limiter: RateLimiter, say: Callable[[str], None], host: str,
                            os_family: str, admin: str, password: str) -> bool:
    acct = cfg.service_account.name
    remote = windows_command(cfg) if os_family == "windows" else linux_command(cfg, sudo="sudo -S -p ''")
    with Secret(password) as sec:
        env = dict(os.environ, SSH_ASKPASS=sec.askpass(), SSH_ASKPASS_REQUIRE="force", DISPLAY=":0")
        r = subprocess.run([*ssh.base(cfg, batch=False), "-o", "PubkeyAuthentication=no",
                            "-o", "NumberOfPasswordPrompts=1", "-l", admin, host, remote],
                           input=(password + "\n") if os_family != "windows" else "",
                           capture_output=True, text=True, timeout=600, env=env)
    for line in (r.stdout + r.stderr).splitlines():
        if line.strip():
            say("  " + line)
    if r.returncode == 0:
        return True
    if "Permission denied" in r.stderr:
        limiter.fail()
        say("!! the machine refused the admin account's password (or password logins are disabled; "
            "use \"I'll run a command on the machine\" instead)")
        raise PasswordRefused
    elif r.returncode == 3:
        say(f"!! the machine's sshd only admits certain users/groups; allow {acct} there "
            f"(e.g. an sshd_config.d drop-in 'AllowGroups {acct}'), then try again")
    say(f"!! bootstrap failed (rc {r.returncode})")
    return False


def _verify_service_login(cfg: Config, host: str, os_family: str, say: Callable[[str], None]) -> bool:
    acct = cfg.service_account.name
    if os_family == "windows":
        r = ssh.run_as_service(cfg, host, "(Get-CimInstance Win32_OperatingSystem).Caption; "
                                          "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]"
                                          "::GetCurrent()).IsInRole('Administrators')")
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or not lines or lines[-1].strip() != "True":
            say(f"!! key login as {acct} failed or it is not an administrator: {(r.stderr or r.stdout).strip()[:300]}")
            return False
        say(f"{lines[0].strip()}: key login works and {acct} is an administrator")
        return True
    probe = ('if [ -r /etc/os-release ]; then . /etc/os-release; echo "$ID|$ID_LIKE|$PRETTY_NAME"; '
             'elif command -v sw_vers >/dev/null; then '
             'echo "macos||$(sw_vers -productName) $(sw_vers -productVersion)"; '
             'else echo "$(uname -s | tr A-Z a-z)||$(uname -sr)"; fi; sudo -n true && echo sudo-ok')
    r = ssh.run_as_service(cfg, host, probe)
    if r.returncode != 0 or "sudo-ok" not in r.stdout:
        say(f"!! key login or sudo as {acct} failed: {(r.stderr or r.stdout).strip()[:300]}")
        return False
    os_id, os_like, pretty = [*r.stdout.splitlines()[0].split("|"), "", "", ""][:3]
    say(f"{pretty}: key login and passwordless sudo work")
    if os_id not in SUPPORTED and not any(x in os_like.split() for x in SUPPORTED):
        say(f"note: {pretty} is not one of the known systems ({', '.join(SUPPORTED)}); "
            "status and patching may be limited")
    return True


def _ssh_with_password(cfg: Config, host: str, admin: str, password: str, remote: str,
                       stdin: str = "") -> subprocess.CompletedProcess[str]:
    """Run `remote` on `host` as `admin`, signing in with the password (never stored or logged)."""
    with Secret(password) as sec:
        env = dict(os.environ, SSH_ASKPASS=sec.askpass(), SSH_ASKPASS_REQUIRE="force", DISPLAY=":0")
        return subprocess.run([*ssh.base(cfg, batch=False), "-o", "PubkeyAuthentication=no",
                               "-o", "NumberOfPasswordPrompts=1", "-l", admin, host, remote],
                              input=stdin, capture_output=True, text=True, timeout=120, env=env)


# Prints admin (an elevated administrator), limited (in Administrators, but Windows gave this remote
# sign-in a filtered token: UAC remote restrictions on local accounts) or no. Sent base64-encoded so
# it works whatever the SSH server's default shell is (cmd or PowerShell).
_ADMIN_PS = ("$c = [Security.Principal.WindowsIdentity]::GetCurrent().Claims; $a = 'S-1-5-32-544'; "
             "if ($c | Where-Object { $_.Type -like '*/groupsid' -and $_.Value -eq $a }) { 'admin' } "
             "elseif ($c | Where-Object { $_.Type -like '*/denyonlysid' -and $_.Value -eq $a }) { 'limited' } "
             "else { 'no' }")
WINDOWS_ADMIN_PROBE = ("powershell -NoProfile -NonInteractive -EncodedCommand "
                       + base64.b64encode(_ADMIN_PS.encode("utf-16-le")).decode())


def _port_open(addr: str, port: int = 22, timeout: float = 8) -> bool:
    try:
        with socket.create_connection((addr, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_access(cfg: Config, limiter: RateLimiter, host: str, os_family: str, method: str,
                admin: str = "", password: str = "") -> list[dict[str, Any]]:
    """The checks the Add machine wizard shows before adding anything, in order:
    [{"step", "ok", "detail"}]. It stops at the first failure. Nothing on the machine changes;
    its host key is recorded (trusted on first use, as adding it would)."""
    out: list[dict[str, Any]] = []

    def step(name: str, ok: bool, detail: str) -> bool:
        out.append({"step": name, "ok": ok, "detail": detail})
        return ok

    family = cfg.service_account.address_family
    addr = ssh.resolves(host, family)
    if not step("Name", bool(addr), f"{host} is {addr}" if addr else
                (f"{host} has no {ssh.FAMILY_NAME[family]} address" if ssh.resolves(host) else
                 f"{host} is not in DNS; check the name, or use its IP address")):
        return out
    seen: list[str] = []
    # the port is tested even when the host key is already on file (a firewall may close it later)
    if not _port_open(addr) or not _record_host_key(cfg, host, seen.append):
        hint = ("Windows has no SSH server until it is turned on: choose \"Run a line on it instead\", which "
                "turns it on" if os_family == "windows" else
                "Remote Login is off: turn it on in System Settings > General > Sharing" if os_family == "macos"
                else "is the SSH server installed and running, and is port 22 open to this server?")
        step("SSH", False, f"nothing answers on port 22. {hint[0].upper() + hint[1:]}")
        return out
    fp = next((w for line in seen if line.startswith("host key:") for w in line.split() if w.startswith("SHA256:")), "")
    step("SSH", True, f"the SSH server answers{' (' + fp + ')' if fp else ''}")
    acct = cfg.service_account.name
    if method == "password":
        if os_family == "windows":
            r = _ssh_with_password(cfg, host, admin, password, WINDOWS_ADMIN_PROBE)
        else:
            r = _ssh_with_password(cfg, host, admin, password, "echo signed-in; sudo -S -p '' -v && echo sudo-ok",
                                   password + "\n")
        if "Permission denied" in r.stderr:
            limiter.fail()
            step("Sign-in", False, f"{host} refused {admin}'s password" + (
                " (Windows wants the account's password, not a PIN; for a domain account try DOMAIN\\user). "
                "Or run a line on it instead: no password needed" if os_family == "windows" else
                " (or allows no password sign-in: then run a line on it instead)"))
            return out
        signed_in = r.returncode == 0 or "signed-in" in r.stdout or r.stdout.strip() in ("admin", "limited", "no")
        if not step("Sign-in", signed_in, f"{admin} can sign in" if signed_in else
                    f"could not sign in: {(r.stderr or r.stdout).strip()[:200]}"):
            return out
        if os_family == "windows":
            got = r.stdout.strip().splitlines()[-1:] or [""]
            rights = {"admin": f"{admin} is an administrator",
                      "limited": f"{admin} is an administrator, but Windows limits a local account signing in "
                                 "remotely (UAC), so setup cannot run this way. Use a domain administrator or the "
                                 "built-in Administrator, or run a line on it instead"}
            step("Rights", got[0] == "admin", rights.get(got[0], f"{admin} is not an administrator on {host}"))
        else:
            step("Rights", "sudo-ok" in r.stdout, f"{admin} can use sudo" if "sudo-ok" in r.stdout
                 else f"{admin} cannot use sudo on {host} (with that password)")
        return out
    # the command or join way: the service account must already work
    ok = _verify_service_login(cfg, host, "windows" if os_family == "windows" else "linux", seen.append)
    step("Sign-in", ok, f"{acct} signs in with Patchyard's key" if ok else
         f"{acct} cannot sign in yet: not set up for Patchyard")
    return out


def _add_one(cfg: Config, limiter: RateLimiter, say: Callable[[str], None], host: str, os_family: str,
             method: str, admin: str, password: str) -> str:
    """Steps 1-5 for one machine: "added", "failed" or "refused" (the admin password was rejected)."""
    acct = cfg.service_account.name
    how = {"password": f"with {admin}'s credentials (used once, not kept)",
           "command": "after the pasted command", "join": "after its join command"}[method]
    say(f"=== Adding {host} ({os_family}), service account {acct} {how}")

    say("--- 1. name and SSH")
    family = cfg.service_account.address_family
    addr = ssh.resolves(host, family)
    if not addr:
        other = ssh.resolves(host)
        if other:
            say(f"!! the name has no {ssh.FAMILY_NAME[family]} address (only {other}), and SSH is set to "
                f"{family} (service_account.address_family); add the address to DNS or use the IP address")
        else:
            say("!! the name does not resolve; add it to DNS (or use its IP address)")
        return "failed"
    say(f"resolves to {addr}")

    say("--- 2. host key")
    if not _record_host_key(cfg, host, say):
        if os_family == "windows" and method == "password":
            say(f"   Windows has no SSH server until it is turned on. Use Add machine > \"I'll run a command on "
                f"it\" instead: paste that command into an administrator PowerShell on {host}. It installs "
                "Microsoft's OpenSSH Server, then press Verify & add.")
        elif method == "command":
            say(f"   Did the command on {host} finish with \"OK: {acct} ready\"? If it stopped with an error, "
                "fix what it says and run it again (it is safe to repeat).")
        return "failed"

    if method == "password":
        say(f"--- 3. create {acct} (as {admin})")
        try:
            if not _run_bootstrap_over_ssh(cfg, limiter, say, host, os_family, admin, password):
                return "failed"
        except PasswordRefused:
            return "refused"
        except ValueError as e:
            say(f"!! {e}")
            return "failed"
    else:
        say(f"--- 3. ({acct} was created by the command run on the machine)")

    say(f"--- 4. {acct} key login")
    if not _verify_service_login(cfg, host, os_family, say):
        if method != "password":
            say("   Did the command finish with \"OK: ... ready\"? Run it again on the machine, then retry.")
        return "failed"

    say("--- 5. inventory")
    if os_family == "windows":
        hv = cfg.state / "host_vars" / f"{host}.yml"
        if not hv.exists():
            hv.write_text(f"# {host}: Windows, added by Patchyard {time.strftime('%Y-%m-%d')}.\n"
                          "ansible_shell_type: powershell\n"
                          "ansible_become: false        # the service account is an administrator\n")
            say(f"wrote {hv.name}: PowerShell over SSH, no sudo")
    inventory.add(cfg.inventory_file, cfg.inventory.managed_group, host)
    say(f"added {host} to [{cfg.inventory.managed_group}] (backup next to the inventory)")
    return "added"


def enroll_many(cfg: Config, jobs: JobManager, limiter: RateLimiter, log: IO[str],
                items: list[tuple[str, str]], *, method: str = "password", admin: str = "",
                password: str = "") -> int:
    """Add machines one after another ([(host, os_family)]), then apply the automatic-update
    policy to all that were added and check once. With a shared admin password the run stops
    at the first refusal, so a typo cannot lock the account by repeating it on every machine."""
    def say(msg: str) -> None:
        log.write(msg + "\n")
        log.flush()

    added, failed = [], []
    for n, (host, os_family) in enumerate(items):
        if len(items) > 1:
            say(f"\n##### {n + 1} of {len(items)}: {host}")
        result = _add_one(cfg, limiter, say, host, os_family, method, admin, password)
        if result == "added":
            added.append(host)
            continue
        failed.append(host)
        if result == "refused" and n + 1 < len(items):
            rest = [h for h, _ in items[n + 1:]]
            say(f"!! stopping: the password was refused, so it is not tried on {', '.join(rest)}")
            failed += rest
            break
    rc = 0
    if added:
        say(f"\n--- 6. automatic updates ({', '.join(h.split('.')[0] for h in added)})")
        rc = jobs.playbook(log, "auto-updates.yml", added, [])
        jobs.check(log)
    names = lambda hs: ", ".join(h.split(".")[0] for h in hs) or "none"     # noqa: E731
    if len(items) > 1 or failed:
        say(f"\n=== added: {names(added)}" + (f" | not added: {names(failed)} (see above)" if failed else ""))
    if added and rc:
        say("!! applying the automatic-update policy FAILED (above). Retry with Auto-update policy.")
    elif len(items) == 1 and added:
        say(f"=== {added[0].split('.')[0]} added")
    return 0 if not failed and rc == 0 else 1


def enroll(cfg: Config, jobs: JobManager, limiter: RateLimiter, log: IO[str], host: str, *,
           os_family: str = "linux", method: str = "password", admin: str = "", password: str = "") -> int:
    """Add one machine. method "password": run the bootstrap with an admin account's password.
    method "command"/"join": the bootstrap already ran on the machine; only verify."""
    return enroll_many(cfg, jobs, limiter, log, [(host, os_family)], method=method, admin=admin, password=password)


def unenroll(cfg: Config, jobs: JobManager, log: IO[str], host: str) -> int:
    inventory.remove(cfg.inventory_file, cfg.inventory.managed_group, host)
    log.write(f"removed {host} from [{cfg.inventory.managed_group}] (backup next to the inventory)\n")
    hv = cfg.state / "host_vars" / f"{host}.yml"
    windows = hv.exists() and "ansible_shell_type: powershell" in hv.read_text()
    if hv.exists():
        retired = cfg.state / "host_vars" / "retired"
        retired.mkdir(exist_ok=True)
        dst = retired / f"{hv.name}.{time.strftime('%Y%m%d%H%M')}"
        hv.rename(dst)
        log.write(f"moved its host_vars to {dst}\n")
    machines.forget(cfg, host)
    acct = cfg.service_account.name
    log.write(f"Nothing on the machine changed: automatic updates keep running and {acct} stays.\n")
    if windows:
        log.write(f"Retiring it? There, in an administrator PowerShell: Remove-LocalUser {acct}; "
                  "Remove-NetFirewallRule -Name Patchyard-SSH; and delete its line from "
                  "C:\\ProgramData\\ssh\\administrators_authorized_keys\n")
    else:
        log.write(f"Retiring it? There: sudo userdel -r {acct}; sudo rm /etc/sudoers.d/90-{acct}\n")
    jobs.check(log, push=False)
    return 0
