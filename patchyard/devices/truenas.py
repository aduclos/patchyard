"""TrueNAS (SCALE and CORE), through the REST API v2.0.

Credentials: {"api_key": "..."} (Credentials > API Keys).

  status   GET  /api/v2.0/system/info              version, system_product
           POST /api/v2.0/update/check_available   {"status": "AVAILABLE"|"UNAVAILABLE", "version": ...}
  update   POST /api/v2.0/update/update {"reboot": true}   returns a job id; followed via core/get_jobs
  reboot   POST /api/v2.0/system/reboot
"""
from __future__ import annotations

import time
from typing import IO, Any

from . import DeviceStatus, Driver, FormField


class TrueNAS(Driver):
    supports_update = True
    supports_reboot = True
    required_credentials = ("api_key",)
    title = "TrueNAS SCALE or CORE"
    help = "An API key (Credentials > API Keys > Add)."
    fields = (FormField("api_key", "API key", secret=True),)
    poll_s = 10.0

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.http.headers["Authorization"] = f"Bearer {self.creds['api_key']}"

    def status(self, check: bool = False) -> DeviceStatus:
        info = self.http.json("GET", "/api/v2.0/system/info")
        avail = self.http.json("POST", "/api/v2.0/update/check_available", body={})
        state = str(avail.get("status", "")) if isinstance(avail, dict) else ""
        version = str(info.get("version", ""))
        available = {"AVAILABLE": True, "UNAVAILABLE": False}.get(state)
        latest = (avail.get("version") if available else version) if available is not None else None
        return DeviceStatus(model=str(info.get("system_product") or "TrueNAS"), version=version,
                            latest=str(latest) if latest else None, update_available=available,
                            notes=[f"update status: {state}"] if state and available is None else [])

    def update(self, log: IO[str]) -> int:
        st = self.status()
        if not st.update_available:
            log.write(f"{self.dev.name}: no update available ({st.version})\n")
            return 0
        log.write(f"{self.dev.name}: {st.version} -> {st.latest} (the system reboots when done)\n")
        job = self.http.json("POST", "/api/v2.0/update/update", body={"reboot": True})
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            try:
                rows = self.http.json("GET", f"/api/v2.0/core/get_jobs?id={int(job)}")
            except Exception:
                log.write("(not answering; probably rebooting)\n")
                return 0
            j = rows[0] if isinstance(rows, list) and rows else {}
            prog = j.get("progress") or {}
            log.write(f"{j.get('state', '?')} {prog.get('percent', '')}% {prog.get('description', '')}\n")
            log.flush()
            if j.get("state") in ("SUCCESS", "FAILED", "ABORTED"):
                return 0 if j["state"] == "SUCCESS" else 1
            time.sleep(self.poll_s)
        return 1

    def reboot(self, log: IO[str]) -> int:
        log.write(f"{self.dev.name}: rebooting\n")
        self.http.json("POST", "/api/v2.0/system/reboot", body={})
        return 0
