"""Join commands: machines add themselves.

A signed-in person creates a join token (Settings > Join commands, or Add machine). It is shown
once, inside two one-liners:

  Linux/BSD:  curl -fsSL https://<dashboard>/join/<token>/linux | sudo sh
  Windows:    irm https://<dashboard>/join/<token>/windows | iex     (administrator PowerShell)

The script creates the service account exactly like the paste-a-command method, then calls
back (POST /join/<token>/done) with the machine's name, and Patchyard verifies its key login and
adds it like any other machine. The token is the only credential on those URLs: it expires, it
is good for a set number of machines, only its SHA-256 is stored (<state>/join-tokens.json),
and every use is audited. A machine is added only after its key login as the service account
works, so a token can only add a machine that really installed Patchyard's key.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import enroll
from .config import Config

TOKEN_RE = re.compile(r"^pyj_([0-9a-f]{8})_([A-Za-z0-9_-]{20,64})$")
HOURS = (1, 24, 168)
MAX_USES = 500


class JoinError(ValueError):
    pass


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class JoinTokens:
    def __init__(self, state: Path):
        self.path = state / "join-tokens.json"

    @contextmanager
    def _locked(self):
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)    # two machines calling back at once must not both win
            yield

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, items: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(items, fh, indent=1)
        tmp.replace(self.path)

    def create(self, name: str, hours: int, uses: int, user: str) -> tuple[str, dict[str, Any]]:
        """A new token ("pyj_<id>_<secret>", shown once) and its record. uses 0 = no limit."""
        if hours not in HOURS:
            raise JoinError(f"valid for {', '.join(map(str, HOURS))} hours")
        if not 0 <= uses <= MAX_USES:
            raise JoinError(f"machines: 1 to {MAX_USES}, or 0 for no limit")
        tid, secret = secrets.token_hex(4), secrets.token_urlsafe(24)
        now = time.time()
        rec = {"id": tid, "name": name.strip()[:60] or f"join {time.strftime('%Y-%m-%d %H:%M')}",
               "hash": _hash(secret), "created": now, "expires": now + hours * 3600, "max_uses": uses or None,
               "created_by": user, "used": [], "revoked": False}
        with self._locked():
            items = [t for t in self._read() if t["expires"] > now - 7 * 86400]   # forget week-old ones
            items.append(rec)
            self._write(items)
        return f"pyj_{tid}_{secret}", rec

    def _valid(self, rec: dict[str, Any], now: float) -> bool:
        left = None if rec["max_uses"] is None else rec["max_uses"] - len(rec["used"])
        return not rec["revoked"] and rec["expires"] > now and (left is None or left > 0)

    def check(self, token: str) -> dict[str, Any] | None:
        """The token's record if it can still add a machine, else None."""
        m = TOKEN_RE.match(token or "")
        if not m:
            return None
        rec = next((t for t in self._read() if t["id"] == m.group(1)), None)
        if not rec or not secrets.compare_digest(rec["hash"], _hash(m.group(2))):
            return None
        return rec if self._valid(rec, time.time()) else None

    def use(self, token: str, host: str, source: str) -> dict[str, Any]:
        """Record that `host` joined with `token` (one use). JoinError when it cannot."""
        m = TOKEN_RE.match(token or "")
        with self._locked():
            items = self._read()
            rec = next((t for t in items if m and t["id"] == m.group(1)), None)
            ok = rec is not None and secrets.compare_digest(rec["hash"], _hash(m.group(2)))
            if not ok or not self._valid(rec, time.time()):
                raise JoinError("this join command has expired, been used up or been revoked")
            rec["used"].append({"host": host, "time": time.time(), "from": source})
            self._write(items)
            return rec

    def attach_job(self, tid: str, host: str, job_id: str) -> None:
        """Note the job adding `host`, so the dashboard can follow it."""
        self._edit_use(tid, host, lambda used, u: u.update(job=job_id))

    def release(self, tid: str, host: str) -> None:
        """Give back the use of a machine whose adding could not start: the same line can run again."""
        self._edit_use(tid, host, lambda used, u: used.remove(u))

    def _edit_use(self, tid: str, host: str, change: Any) -> None:
        with self._locked():
            items = self._read()
            rec = next((t for t in items if t["id"] == tid), None)
            uses = reversed(rec["used"]) if rec else []
            use = next((u for u in uses if u["host"] == host and "job" not in u), None)
            if use is not None:
                change(rec["used"], use)
                self._write(items)

    def revoke(self, tid: str) -> None:
        with self._locked():
            items = self._read()
            rec = next((t for t in items if t["id"] == tid), None)
            if not rec:
                raise JoinError("no such join command")
            rec["revoked"] = True
            self._write(items)

    def public(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for t in sorted(self._read(), key=lambda t: t["created"], reverse=True):
            left = None if t["max_uses"] is None else t["max_uses"] - len(t["used"])
            out.append({k: t[k] for k in ("id", "name", "created", "expires", "max_uses", "created_by", "revoked")}
                       | {"used": [u["host"] for u in t["used"]], "left": left, "active": self._valid(t, now),
                          "jobs": {u["host"]: u["job"] for u in t["used"] if u.get("job")}})
        return out


# ------------------------------------------------------------------ the scripts and commands
def base_url(cfg: Config) -> str:
    return cfg.site.url.rstrip("/")


def commands(cfg: Config, token: str) -> dict[str, str]:
    url = f"{base_url(cfg)}/join/{token}"
    return {"linux": f"curl -fsSL {url}/linux | sudo sh",
            # TLS 1.2 first: older Windows PowerShell does not offer it by default
            "windows": f"[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; irm {url}/windows | iex"}


def linux_script(cfg: Config, token: str, rec: dict[str, Any]) -> str:
    acct, pub, sources = enroll.bootstrap_values(cfg)
    body = base64.b64encode((cfg.code / "bootstrap" / "bootstrap.sh").read_bytes()).decode()
    done = f"{base_url(cfg)}/join/{token}/done"
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["expires"]))
    return f"""#!/bin/sh
# Patchyard join script for {cfg.site.name}: creates the {acct} account Patchyard manages this
# machine with (key login from the Patchyard server only, passwordless sudo), then asks
# {base_url(cfg)} to add the machine. This join command works until {until}.
set -u
if [ "$(id -u)" -ne 0 ]; then
  echo "run it as root:  curl -fsSL {base_url(cfg)}/join/<token>/linux | sudo sh" >&2
  exit 1
fi
B=$(mktemp) || exit 1
echo {body} | base64 -d > "$B"
env PATCHYARD_ACCOUNT={shlex.quote(acct)} PATCHYARD_PUBKEY={shlex.quote(pub)} \\
    PATCHYARD_ALLOWED_SOURCES={shlex.quote(sources)} sh "$B"
rc=$?
rm -f "$B"
if [ "$rc" -ne 0 ]; then
  echo "setting up {acct} failed (exit $rc): fix what it says and run the command again" >&2
  exit "$rc"
fi
H=$(hostname -f 2>/dev/null || hostname)
echo "asking Patchyard to add $H"
BODY="{{\\"host\\": \\"$H\\", \\"os\\": \\"linux\\"}}"
if command -v curl >/dev/null 2>&1; then
  curl -sS -H 'Content-Type: application/json' --data "$BODY" '{done}'
else
  wget -qO- --header='Content-Type: application/json' --post-data="$BODY" '{done}'
fi
echo
"""


def windows_script(cfg: Config, token: str, rec: dict[str, Any]) -> str:
    acct, pub, sources = enroll.bootstrap_values(cfg)
    done = f"{base_url(cfg)}/join/{token}/done"
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["expires"]))
    return (f"# Patchyard join script for {cfg.site.name}: installs Microsoft's OpenSSH Server if needed, creates the\n"
            f"# {acct} account (key login from the Patchyard server only), then asks {base_url(cfg)} to add\n"
            f"# this machine. Run in an administrator PowerShell. Works until {until}.\n"
            f"$env:PATCHYARD_ACCOUNT='{acct}'\n$env:PATCHYARD_PUBKEY='{pub}'\n"
            f"$env:PATCHYARD_ALLOWED_SOURCES='{sources}'\n"
            + enroll._compact_ps((cfg.code / "bootstrap" / "bootstrap.ps1").read_text())
            + "$h = [System.Net.Dns]::GetHostEntry([string]$env:COMPUTERNAME).HostName.ToLower()\n"  # site-data: ok
            + 'Write-Output "asking Patchyard to add $h"\n'
            + "$body = @{host = $h; os = 'windows'} | ConvertTo-Json\n"
            + f"try {{ (Invoke-WebRequest -UseBasicParsing -Method Post -Uri '{done}' -ContentType 'application/json' "
              "-Body $body).Content }\n"
            + "catch { if ($_.ErrorDetails) { $_.ErrorDetails.Message } else { $_.Exception.Message } }\n")


def refusal_script(os_family: str) -> str:
    """What an expired or unknown token gets instead of a script: a clear message, nothing done."""
    msg = "This Patchyard join command has expired, been used up or been revoked; create a new one on the dashboard."
    return f"throw '{msg}'\n" if os_family == "windows" else f"#!/bin/sh\necho '{msg}' >&2\nexit 1\n"
