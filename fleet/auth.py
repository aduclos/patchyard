"""Sign-in: verify a person's password and that they belong to the configured group.

Backends (config `auth.method`):
  kerberos  password checked with kinit against `auth.realm` (into a throw-away
            in-memory credential cache); works for Active Directory, FreeIPA, MIT KDCs
  none      no sign-in at all; only for a trusted, single-user, local-only setup

Group membership is resolved through NSS (the same source `id` and `getent`
use), so AD via SSSD, LDAP via nslcd, and plain local groups all work.
"""
from __future__ import annotations

import grp
import os
import pwd
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Config

USERNAME_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]{0,63}$")


def normalize(username: str) -> str:
    """'DOMAIN\\user', 'user@realm' and 'User' all become 'user'."""
    u = username.strip().lower()
    if "\\" in u:
        u = u.split("\\", 1)[1]
    return u.split("@", 1)[0]


def in_group(user: str, group: str) -> bool:
    """True if `user` is a member of `group` (primary, supplementary, or listed member).

    Both views are consulted because directory caches (e.g. SSSD's initgroups) can
    lag behind a membership change while the group's own member list is current.
    """
    try:
        g = grp.getgrnam(group)
    except KeyError:
        return False
    if user in (m.lower() for m in g.gr_mem):
        return True
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return False
    return g.gr_gid == pw.pw_gid or g.gr_gid in os.getgrouplist(user, pw.pw_gid)


@dataclass
class RateLimiter:
    """N failures within a window lock everyone out for a while (directories often have no lockout)."""
    max_failures: int
    window_s: float
    lockout_s: float
    clock: Callable[[], float] = time.time
    _fails: list[float] = field(default_factory=list)
    _locked_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def locked_for(self) -> float:
        return max(0.0, self._locked_until - self.clock())

    def fail(self) -> None:
        with self._lock:
            now = self.clock()
            self._fails = [t for t in self._fails if now - t < self.window_s] + [now]
            if len(self._fails) >= self.max_failures:
                self._locked_until = now + self.lockout_s
                self._fails.clear()


class AuthError(Exception):
    """Sign-in refused; the message is safe to show to the user."""


class Authenticator:
    def __init__(self, cfg: Config, limiter: RateLimiter | None = None,
                 check_password: Callable[[str, str], bool] | None = None,
                 member: Callable[[str, str], bool] = in_group):
        self.cfg = cfg
        a = cfg.auth
        self.limiter = limiter or RateLimiter(a.max_failures, a.failure_window_minutes * 60, a.lockout_minutes * 60)
        self._check_password = check_password or self._kinit
        self._member = member

    @property
    def required(self) -> bool:
        return self.cfg.auth.method != "none"

    def authenticate(self, username: str, password: str) -> str:
        """Return the normalized user name, or raise AuthError."""
        if not self.required:
            return normalize(username) or "anonymous"
        wait = self.limiter.locked_for()
        if wait:
            raise AuthError(f"too many failed sign-ins; try again in {int(wait // 60) + 1} min")
        user = normalize(username)
        if not USERNAME_RE.match(user) or not password or not self._check_password(user, password):
            self.limiter.fail()
            raise AuthError("wrong username or password")
        if not self._member(user, self.cfg.auth.group):
            raise AuthError(f"{user} is not in the group {self.cfg.auth.group}")
        return user

    def is_member(self, user: str, group: str | None = None) -> bool:
        return self._member(user, group or self.cfg.auth.group)

    def _kinit(self, user: str, password: str) -> bool:
        env = dict(os.environ, KRB5CCNAME="MEMORY:ansiblefleet")
        try:
            r = subprocess.run(["kinit", f"{user}@{self.cfg.auth.realm}"], input=password + "\n",
                               capture_output=True, text=True, timeout=20, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0
