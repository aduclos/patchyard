"""Synology DSM, through the Web API.

Credentials: {"username": "...", "password": "..."} of an administrator WITHOUT two-factor
sign-in (a dedicated account is best). DSM installs its own updates from Control Panel
(or automatically); this driver reports whether one is waiting and can reboot.

  login    GET /webapi/entry.cgi?api=SYNO.API.Auth&version=6&method=login  -> sid
  status   SYNO.DSM.Info getinfo (model, version_string); SYNO.Core.Upgrade.Server check
  reboot   SYNO.Core.System reboot
  logout   SYNO.API.Auth logout
"""
from __future__ import annotations

import contextlib
from typing import IO, Any
from urllib.parse import urlencode

from . import DeviceAuthError, DeviceError, DeviceStatus, Driver

AUTH_ERRORS = {"400": "wrong username or password", "401": "account disabled", "402": "permission denied",
               "403": "two-factor sign-in is required for this account; use one without it",
               "404": "two-factor code failed", "407": "this address is blocked by DSM after failed logins"}


class Synology(Driver):
    supports_update = False
    supports_reboot = True
    required_credentials = ("username", "password")
    title = "Synology NAS (DSM)"
    help = ("An administrator without two-factor sign-in; a dedicated account is best. DSM installs its own "
            "updates: Patchyard shows when one waits and can reboot.")

    def _call(self, api: str, version: int, method: str, **params: Any) -> dict[str, Any]:
        q = {"api": api, "version": version, "method": method, **params}
        if getattr(self, "sid", None):
            q["_sid"] = self.sid
        reply = self.http.json("GET", "/webapi/entry.cgi?" + urlencode(q))
        if not reply.get("success"):
            code = str((reply.get("error") or {}).get("code", "?"))
            raise DeviceError(f"{api}.{method}: error {code}")
        return reply.get("data") or {}

    def __enter__(self) -> Synology:
        q = {"api": "SYNO.API.Auth", "version": 6, "method": "login", "account": self.creds["username"],
             "passwd": self.creds["password"], "format": "sid", "session": "patchyard"}
        reply = self.http.json("GET", "/webapi/entry.cgi?" + urlencode(q))
        if not reply.get("success"):
            code = str((reply.get("error") or {}).get("code", "?"))
            raise DeviceAuthError(AUTH_ERRORS.get(code, f"login refused (error {code})"))
        self.sid = reply["data"]["sid"]
        return self

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(DeviceError):
            self._call("SYNO.API.Auth", 6, "logout", session="patchyard")

    def status(self, check: bool = False) -> DeviceStatus:
        with self:
            info = self._call("SYNO.DSM.Info", 2, "getinfo")
            try:
                up = self._call("SYNO.Core.Upgrade.Server", 1, "check").get("update") or {}
            except DeviceError as e:
                up, note = {}, [f"update check unavailable: {e}"]
            else:
                note = []
        version = str(info.get("version_string") or info.get("version") or "")
        available = up.get("available") if "available" in up else None
        latest = (up.get("version") or None) if available else (version if available is False else None)
        return DeviceStatus(model=str(info.get("model") or "Synology"), version=version, latest=latest,
                            update_available=available,
                            notes=note or ["install updates from DSM (Control Panel > Update)"])

    def reboot(self, log: IO[str]) -> int:
        with self:
            log.write(f"{self.dev.name}: rebooting\n")
            self._call("SYNO.Core.System", 1, "reboot")
        return 0
