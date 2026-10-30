"""Ubiquiti UniFi devices (access points, switches, gateways), through the UniFi Network
application that manages them. One [[devices]] entry per UniFi device.

Credentials: {"username": "...", "password": "..."} of a local controller account.
Options:
  mac       the device's MAC address (required)
  site      controller site name (default "default")
  unifi_os  "true" for consoles running UniFi OS (UDM, Cloud Key Gen2+, UniFi OS Server),
            "false" for the classic self-hosted Network application (default "true")

  status   GET  {p}/api/s/{site}/stat/device/{mac}   model, version, upgradable, upgrade_to_firmware
  update   POST {p}/api/s/{site}/cmd/devmgr  {"cmd": "upgrade", "mac": ...}
  reboot   POST {p}/api/s/{site}/cmd/devmgr  {"cmd": "restart", "mac": ...}
"""
from __future__ import annotations

import base64
import contextlib
import json
from typing import IO, Any

from . import PASSWORD, USER, DeviceAuthError, DeviceError, DeviceStatus, Driver, FormField


class UniFi(Driver):
    supports_update = True
    supports_reboot = True
    required_credentials = ("username", "password")
    title = "Ubiquiti UniFi device (through UniFi Network)"
    help = ("The address is the UniFi Network application or console, not the device. Use a local account; "
            "add one entry per device.")
    fields = (USER, PASSWORD,
              FormField("mac", "Device MAC address", option=True, help="Shown for the device in UniFi Network"),
              FormField("site", "Site", option=True, required=False, default="default"),
              FormField("unifi_os", "Console", option=True, required=False, default="true", choices=("true", "false"),
                        help="true: UDM, Cloud Key Gen2+ or UniFi OS Server; "
                             "false: the classic self-hosted application"))

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        opts = self.dev.options
        if not opts.get("mac"):
            raise DeviceError("unifi: options.mac (the device's MAC address) is required")
        self.mac = opts["mac"].lower()
        self.site = opts.get("site", "default")
        self.unifi_os = opts.get("unifi_os", "true").lower() != "false"
        self.prefix = "/proxy/network" if self.unifi_os else ""

    def __enter__(self) -> UniFi:
        path = "/api/auth/login" if self.unifi_os else "/api/login"
        code, _ = self.http.request("POST", path, body={"username": self.creds["username"],
                                                          "password": self.creds["password"]})
        if code in (400, 401, 403):
            raise DeviceAuthError("login refused (wrong credentials, or a cloud-only account)")
        if code >= 400:
            raise DeviceError(f"login: HTTP {code}")
        for c in self.http.cookies:            # UniFi OS wants the CSRF token from the session cookie back
            if c.name == "TOKEN":
                with contextlib.suppress(ValueError, IndexError):
                    payload = json.loads(base64.urlsafe_b64decode(c.value.split(".")[1] + "=="))
                    if payload.get("csrfToken"):
                        self.http.headers["X-CSRF-Token"] = payload["csrfToken"]
        return self

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(DeviceError):
            self.http.request("POST", "/api/auth/logout" if self.unifi_os else "/api/logout", body={})

    def _device(self) -> dict[str, Any]:
        reply = self.http.json("GET", f"{self.prefix}/api/s/{self.site}/stat/device/{self.mac}")
        rows = reply.get("data") if isinstance(reply, dict) else None
        if not rows:
            raise DeviceError(f"no device {self.mac} on site {self.site}")
        return rows[0]

    def status(self, check: bool = False) -> DeviceStatus:
        with self:
            d = self._device()
        version = str(d.get("version", ""))
        available = bool(d.get("upgradable"))
        latest = d.get("upgrade_to_firmware") if available else version
        return DeviceStatus(model=str(d.get("model") or d.get("type") or "UniFi"), version=version,
                            latest=str(latest) if latest else None, update_available=available,
                            notes=[str(d["name"])] if d.get("name") else [])

    def _cmd(self, cmd: str) -> None:
        self.http.json("POST", f"{self.prefix}/api/s/{self.site}/cmd/devmgr", body={"cmd": cmd, "mac": self.mac})

    def update(self, log: IO[str]) -> int:
        with self:
            d = self._device()
            if not d.get("upgradable"):
                log.write(f"{self.dev.name}: no update available ({d.get('version')})\n")
                return 0
            log.write(f"{self.dev.name}: {d.get('version')} -> {d.get('upgrade_to_firmware')}; "
                      "the controller upgrades it (the device restarts)\n")
            self._cmd("upgrade")
        return 0

    def reboot(self, log: IO[str]) -> int:
        with self:
            log.write(f"{self.dev.name}: restarting\n")
            self._cmd("restart")
        return 0
