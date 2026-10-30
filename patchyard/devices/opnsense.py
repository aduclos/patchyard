"""OPNsense firewalls, through the official REST API.

Credentials: {"key": "...", "secret": "..."}, an API key of a dedicated user whose only
privilege is "System: Firmware" (System > Access > Users > + > API keys).

  status   GET  /api/core/firmware/status       (POST /api/core/firmware/check first if check=True)
  update   POST /api/core/firmware/update        minor updates; follows /upgradestatus
           POST /api/core/firmware/upgrade       when a major upgrade is what is offered
  reboot   POST /api/core/firmware/reboot
"""
from __future__ import annotations

import time
from typing import IO, Any

from . import DeviceError, DeviceStatus, Driver, FormField


class OPNsense(Driver):
    supports_update = True
    supports_reboot = True
    required_credentials = ("key", "secret")
    title = "OPNsense firewall"
    help = ("An API key of a dedicated user whose only privilege is System: Firmware (System > Access > Users, "
            "the user, API keys, +). The file OPNsense downloads holds both values.")
    fields = (FormField("key", "API key"), FormField("secret", "API secret", secret=True))
    poll_s = 5.0

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.http.basic_auth(self.creds["key"], self.creds["secret"])

    def _wait(self, log: IO[str] | None, limit_s: float) -> str:
        """Follow /upgradestatus until the background job ends; returns its final status."""
        deadline, shown = time.monotonic() + limit_s, 0
        while time.monotonic() < deadline:
            try:
                st = self.http.json("GET", "/api/core/firmware/upgradestatus")
            except DeviceError:
                if log:                      # the firewall going away mid-update usually means it rebooted
                    log.write("(firewall not answering; probably rebooting)\n")
                    log.flush()
                return "reboot"
            text = st.get("log", "") or ""
            if log and len(text) > shown:
                log.write(text[shown:])
                log.flush()
                shown = len(text)
            if st.get("status") in ("done", "reboot", "error"):
                return str(st["status"])
            time.sleep(self.poll_s)
        return "timeout"

    def status(self, check: bool = False) -> DeviceStatus:
        if check:
            self.http.json("POST", "/api/core/firmware/check", body={})
            self._wait(None, 180)
        st = self.http.json("GET", "/api/core/firmware/status")
        product = st.get("product") or {}
        version = product.get("product_version") or st.get("product_version") or ""
        latest = product.get("product_latest") or None
        state = st.get("status", "")
        pending = len(st.get("upgrade_packages") or []) + len(st.get("new_packages") or [])
        major = st.get("upgrade_major_version") or ""
        available = state in ("update", "upgrade") or pending > 0 or bool(major)
        notes = [str(st["status_msg"])] if st.get("status_msg") else []
        if major:
            notes.append(f"major upgrade to {major} offered")
            latest = latest or major
        name = product.get("product_name") or st.get("product_name") or ""
        if not name or name == "opnsense":          # the API's product id is lower-case
            name = "OPNsense"
        return DeviceStatus(model=str(name), version=str(version),
                            latest=str(latest) if latest else (str(version) if not available else None),
                            update_available=available,
                            needs_reboot=str(st.get("needs_reboot", "0")) == "1"
                            or str(st.get("upgrade_needs_reboot", "0")) == "1",
                            notes=notes)

    def update(self, log: IO[str]) -> int:
        st = self.status(check=True)
        if not st.update_available:
            log.write(f"{self.dev.name}: no update available ({st.version})\n")
            return 0
        major = any(n.startswith("major upgrade") for n in st.notes)
        endpoint = "/api/core/firmware/upgrade" if major else "/api/core/firmware/update"
        log.write(f"{self.dev.name}: {st.version} -> {st.latest or '?'} via {endpoint}\n")
        log.flush()
        self.http.json("POST", endpoint, body={})
        result = self._wait(log, 3600)
        log.write(f"\n{self.dev.name}: update finished with status '{result}'\n")
        return 0 if result in ("done", "reboot") else 1

    def reboot(self, log: IO[str]) -> int:
        log.write(f"{self.dev.name}: rebooting\n")
        self.http.json("POST", "/api/core/firmware/reboot", body={})
        return 0
