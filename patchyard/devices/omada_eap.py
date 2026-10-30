"""TP-Link Omada access points in standalone mode (no Omada controller), via the web API.

Credentials: {"username": "admin", "password": "..."}. Login works over HTTPS only.

  login    POST /             username=..., password=MD5(password) in upper-case hex -> JSESSIONID
  status   GET  /data/status.device.json?operation=read    model, hardware and firmware version

Standalone EAPs cannot look for new firmware themselves and are updated by uploading a
file, so this driver reports the version (update_available = None) and does not update
or reboot. Compare the version with the vendor's download page for the hardware version.
"""
from __future__ import annotations

import hashlib
import json

from . import DeviceAuthError, DeviceError, DeviceStatus, Driver


class OmadaEAP(Driver):
    supports_update = False
    supports_reboot = False
    required_credentials = ("username", "password")
    title = "TP-Link Omada access point (standalone)"
    help = ("Reports the firmware version: standalone access points cannot look for updates themselves. "
            "Sign-in works over HTTPS only.")

    def _login(self) -> None:
        self.http.headers["Referer"] = self.http.base + "/"
        self.http.request("GET", "/")                     # session cookie
        pw = hashlib.md5(self.creds["password"].encode()).hexdigest().upper()  # the device's own scheme
        code, raw = self.http.request("POST", "/", form={"username": self.creds["username"], "password": pw})
        if code >= 400 or not any(c.name == "JSESSIONID" for c in self.http.cookies):
            raise DeviceAuthError("login refused (wrong credentials, or not over https)")
        try:
            reply = json.loads(raw or b"{}")
        except ValueError:
            reply = {}
        if isinstance(reply, dict) and reply.get("success") is False:
            raise DeviceAuthError("login refused (wrong credentials)")

    def status(self, check: bool = False) -> DeviceStatus:
        self._login()
        reply = self.http.json("GET", "/data/status.device.json?operation=read")
        data = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(data, dict):
            raise DeviceError("status.device.json: unexpected reply")
        model = str(data.get("deviceModel") or data.get("model") or "Omada EAP")
        hw = str(data.get("hardwareVersion") or data.get("hwVersion") or "")
        fw = str(data.get("firmwareVersion") or data.get("fwVersion") or "")
        notes = ["standalone EAPs cannot check for firmware; compare with the vendor site"
                 + (f" (hardware {hw})" if hw else "")]
        return DeviceStatus(model=model, version=fw, latest=None, update_available=None, notes=notes)
