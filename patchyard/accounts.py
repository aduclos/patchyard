"""Patchyard's own sign-in accounts (auth.method = "local") and API tokens.

Both live in the state directory, written only by the service account:
  users.json    {"users": {"alice": {"hash": "pbkdf2_sha256$<iterations>$<salt>$<hash>", "created": ...}}}
  tokens.json   {"tokens": {"<id>": {"name": ..., "user": ..., "hash": "<sha256 of the secret>", "created": ...,
                                     "last_used": ...}}}

Passwords: PBKDF2-HMAC-SHA256, 600 000 iterations, 16-byte random salt, constant-time
comparison. API tokens look like "pyd_<id>_<secret>"; only a SHA-256 of the whole token
is stored, so a leaked tokens.json cannot be replayed. Tokens are shown once, at creation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

ITERATIONS = 600_000
NAME_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]{0,63}$")
MIN_PASSWORD = 10
_lock = threading.Lock()


class AccountError(ValueError):
    """Bad input; the message is safe to show."""


def hash_password(password: str, *, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
    return hmac.compare_digest(calc, base64.b64decode(digest))


def _read(path: Path, key: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data.get(key, {}) if isinstance(data, dict) else {}


def _write(path: Path, key: str, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({key: value}, fh, indent=1, sort_keys=True)
    tmp.replace(path)


class Users:
    def __init__(self, state: Path):
        self.path = state / "users.json"

    def all(self) -> dict[str, Any]:
        return _read(self.path, "users")

    def names(self) -> list[str]:
        return sorted(self.all())

    def verify(self, name: str, password: str) -> bool:
        user = self.all().get(name)
        if not user:
            check_password(password, hash_password("x", iterations=1000))   # same work either way
            return False
        return check_password(password, user.get("hash", ""))

    def set(self, name: str, password: str, *, create: bool) -> None:
        if not NAME_RE.match(name):
            raise AccountError("user names: lower-case letters, digits, _ . - (max 64)")
        if len(password) < MIN_PASSWORD:
            raise AccountError(f"passwords need at least {MIN_PASSWORD} characters")
        with _lock:
            users = self.all()
            if create and name in users:
                raise AccountError(f"{name} already exists")
            if not create and name not in users:
                raise AccountError(f"no user {name}")
            created = users.get(name, {}).get("created", int(time.time()))
            users[name] = {"hash": hash_password(password), "created": created}
            _write(self.path, "users", users)

    def remove(self, name: str) -> None:
        with _lock:
            users = self.all()
            if name not in users:
                raise AccountError(f"no user {name}")
            if len(users) == 1:
                raise AccountError("cannot remove the last user (you would be locked out)")
            del users[name]
            _write(self.path, "users", users)


class Tokens:
    def __init__(self, state: Path):
        self.path = state / "tokens.json"

    def all(self) -> dict[str, Any]:
        return _read(self.path, "tokens")

    def create(self, name: str, user: str) -> str:
        if not name or len(name) > 64:
            raise AccountError("give the token a name (max 64 characters), e.g. 'laptop MCP'")
        tid, secret = secrets.token_hex(4), secrets.token_urlsafe(32)
        token = f"pyd_{tid}_{secret}"
        with _lock:
            tokens = self.all()
            tokens[tid] = {"name": name, "user": user, "hash": hashlib.sha256(token.encode()).hexdigest(),
                           "created": int(time.time()), "last_used": None}
            _write(self.path, "tokens", tokens)
        return token

    def revoke(self, tid: str) -> None:
        with _lock:
            tokens = self.all()
            if tid not in tokens:
                raise AccountError("no such token")
            del tokens[tid]
            _write(self.path, "tokens", tokens)

    def user_for(self, token: str) -> str | None:
        """The user a valid token acts as, else None. Records when it was last used."""
        m = re.fullmatch(r"pyd_([0-9a-f]{8})_[A-Za-z0-9_-]{20,}", token or "")
        if not m:
            return None
        with _lock:
            tokens = self.all()
            entry = tokens.get(m.group(1))
            if not entry or not hmac.compare_digest(entry.get("hash", ""), hashlib.sha256(token.encode()).hexdigest()):
                return None
            now = int(time.time())
            if (entry.get("last_used") or 0) < now - 300:      # don't rewrite the file on every request
                entry["last_used"] = now
                _write(self.path, "tokens", tokens)
            return str(entry.get("user") or "token")

    def public(self) -> list[dict[str, Any]]:
        return [{"id": tid, **{k: v for k, v in t.items() if k != "hash"}} for tid, t in sorted(self.all().items())]
