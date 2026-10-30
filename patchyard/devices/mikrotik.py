"""MikroTik RouterOS 7, through its REST API.

Credentials: {"username": "...", "password": "..."} (a user in a group with read, write
and reboot policy; "full" is not needed).

  status   GET  /rest/system/resource                 board-name, version
           GET  /rest/system/package/update           installed-version, latest-version, status
           POST /rest/system/package/update/check-for-updates   (when check=True)
  update   POST /rest/system/package/update/install   downloads, installs, reboots
  reboot   POST /rest/system/reboot
"""
from __future__ import annotations

from typing import IO, Any

from . import DeviceStatus, Driver


class MikroTik(Driver):
    supports_update = True
    supports_reboot = True
    required_credentials = ("username", "password")
    title = "MikroTik RouterOS 7"
    help = "A user in a group with read, write and reboot policy. The REST API needs the www-ssl service."

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.http.basic_auth(self.creds["username"], self.creds["password"])

    def status(self, check: bool = False) -> DeviceStatus:
        if check:
            self.http.json("POST", "/rest/system/package/update/check-for-updates", body={})
        res = self.http.json("GET", "/rest/system/resource")
        upd = self.http.json("GET", "/rest/system/package/update")
        installed = str(upd.get("installed-version") or res.get("version", "")).split(" ")[0]
        latest = upd.get("latest-version") or None
        available = (latest is not None and latest != installed) if latest else None
        notes = [str(upd["status"])] if upd.get("status") else []
        return DeviceStatus(model=str(res.get("board-name") or "RouterOS"), version=installed,
                            latest=str(latest) if latest else None, update_available=available, notes=notes)

    def update(self, log: IO[str]) -> int:
        st = self.status(check=True)
        if not st.update_available:
            log.write(f"{self.dev.name}: no update available ({st.version})\n")
            return 0
        log.write(f"{self.dev.name}: {st.version} -> {st.latest}; installing (the router reboots)\n")
        self.http.json("POST", "/rest/system/package/update/install", body={})
        return 0

    def reboot(self, log: IO[str]) -> int:
        log.write(f"{self.dev.name}: rebooting\n")
        self.http.request("POST", "/rest/system/reboot", body={})
        return 0
