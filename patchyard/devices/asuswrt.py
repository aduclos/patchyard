"""ASUS routers, mesh systems and access points (ASUSWRT), through the web interface API.

Credentials: {"username": "admin", "password": "..."} (ASUSWRT has no read-only accounts).

  login    POST /login.cgi        login_authorization=base64(user:pass) -> asus_token cookie
  status   POST /appGet.cgi hook=nvram_get(a);nvram_get(b)...   productid, firmver, buildno, extendno,
                                                  webs_state_flag/info (the firmware check result)
  check    POST /apply.cgi rc_service=start_webs_update, then poll webs_state_update
  update   POST /apply.cgi rc_service=start_webs_upgrade (downloads, flashes, reboots)
  reboot   POST /apply.cgi action_mode=reboot
  logout   GET  /Logout.asp       ALWAYS: ASUSWRT allows one admin session at a time
"""
from __future__ import annotations

import base64
import contextlib
import json
import re
import time
from typing import IO

from . import DeviceAuthError, DeviceError, DeviceStatus, Driver

# The vendor app's user agent; the web API answers it with JSON. (site-data: ok, a version number)
UA = "asusrouter-Android-DUTUtil-1.0.0.245"  # site-data: ok
# login.cgi error_status values (ASUSWRT)
LOGIN_ERRORS = {"1": "login locked by the device (too many attempts); wait, then sign in once in a browser",
                "2": "another admin is signed in to the device's web page",
                "3": "wrong username or password",
                "7": "login locked by the device (too many attempts); wait, then sign in once in a browser",
                "10": "the device wants a captcha after failed logins; sign in once in a browser to clear it"}
VARS = ("productid", "firmver", "buildno", "extendno", "webs_state_flag", "webs_state_info", "webs_state_update")


class AsusWrt(Driver):
    supports_update = True
    supports_reboot = True
    required_credentials = ("username", "password")
    title = "ASUS router, mesh node or access point"
    help = ("An administrator account (ASUSWRT has no read-only one). A wrong password counts as a failed "
            "sign-in and after a few the device asks for a captcha; Patchyard then stops trying until the "
            "password changes. Sign out of the device's web page: it allows one admin session at a time.")
    poll_s = 5.0

    def __enter__(self) -> AsusWrt:
        auth = base64.b64encode(f"{self.creds['username']}:{self.creds['password']}".encode()).decode()
        self.http.headers.update({"User-Agent": UA, "Referer": self.http.base + "/"})
        code, raw = self.http.request("POST", "/login.cgi", form={"login_authorization": auth})
        reply: dict = {}
        with contextlib.suppress(ValueError):
            reply = json.loads(raw or b"{}")
        token = reply.get("asus_token") if isinstance(reply, dict) else None
        if code >= 400 or not token:
            err = str(reply.get("error_status", "")) if isinstance(reply, dict) else ""
            msg = LOGIN_ERRORS.get(err, f"login refused (error_status {err or '?'})")
            raise (DeviceError if err == "2" else DeviceAuthError)(msg)
        self.http.headers["Cookie"] = f"asus_token={token}"
        return self

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(DeviceError):
            self.http.request("GET", "/Logout.asp")

    def nvram(self, names: tuple[str, ...] = VARS) -> dict[str, str]:
        # A form body, as the vendor app sends it: in a GET query string the httpd of some
        # firmware ends the hook at the first raw ";", so only the first name came back.
        hook = ";".join(f"nvram_get({n})" for n in names)
        code, raw = self.http.request("POST", "/appGet.cgi", form={"hook": hook})
        if code >= 400:
            raise DeviceError(f"appGet.cgi: HTTP {code}")
        text = raw.decode(errors="replace")
        try:
            data = json.loads(text)
        except ValueError:                       # some firmware returns single-quoted pseudo-JSON
            data = dict(re.findall(r'"?(\w+)"?\s*:\s*"([^"]*)"', text))
        return {k: str(v) for k, v in data.items()}

    def apply(self, form: dict[str, str]) -> None:
        code, _ = self.http.request("POST", "/apply.cgi", form=form)
        if code >= 400:
            raise DeviceError(f"apply.cgi: HTTP {code}")

    @staticmethod
    def version(nv: dict[str, str]) -> str:
        v = ".".join(x for x in (nv.get("firmver", ""), nv.get("buildno", "")) if x)
        return f"{v}_{nv['extendno']}" if nv.get("extendno") else v

    def _status(self, check: bool) -> DeviceStatus:
        if check:
            self.apply({"action_mode": "apply", "rc_service": "start_webs_update"})
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and self.nvram(("webs_state_update",)).get("webs_state_update") != "1":
                time.sleep(self.poll_s)
        nv = self.nvram()
        flag, info = nv.get("webs_state_flag", ""), nv.get("webs_state_info", "")
        available = flag in ("1", "2") if flag else None
        # webs_state_info keeps the last check's result even when nothing is newer, so only an
        # available update takes its version from there; "no update" means latest = installed.
        if available:
            latest = info.replace("_", ".", 1) if info else None
        elif available is False:
            latest = self.version(nv)
        else:
            latest = None
        notes = ["the router has not checked for firmware yet"] if not flag else []
        return DeviceStatus(model=nv.get("productid", "ASUS"), version=self.version(nv), latest=latest,
                            update_available=available, notes=notes)

    def status(self, check: bool = False) -> DeviceStatus:
        with self:
            return self._status(check)

    def update(self, log: IO[str]) -> int:
        with self:
            st = self._status(check=True)
            if not st.update_available:
                log.write(f"{self.dev.name}: no update available ({st.version})\n")
                return 0
            log.write(f"{self.dev.name}: {st.version} -> {st.latest}; downloading and flashing "
                      "(the device reboots by itself, ~5 min)\n")
            log.flush()
            self.apply({"action_mode": "apply", "rc_service": "start_webs_upgrade"})
        return self._wait_back(log, st.version)

    def _wait_back(self, log: IO[str], old: str, limit_s: float = 900) -> int:
        deadline = time.monotonic() + limit_s
        time.sleep(60)
        while time.monotonic() < deadline:
            try:
                st = self.status()
                if st.version != old:
                    log.write(f"{self.dev.name}: back on {st.version}\n")
                    return 0
            except DeviceError:
                pass
            log.write(".")
            log.flush()
            time.sleep(15)
        log.write(f"\n{self.dev.name}: not back on a new version within {int(limit_s / 60)} min; check it\n")
        return 1

    def reboot(self, log: IO[str]) -> int:
        with self:
            log.write(f"{self.dev.name}: rebooting\n")
            self.apply({"action_mode": "reboot"})
        return 0
