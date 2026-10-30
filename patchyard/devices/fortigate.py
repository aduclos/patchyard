"""Fortinet FortiGate firewalls (FortiOS 6.2 and later), through the REST API.

Credentials: {"token": "..."}, the API key of a REST API administrator (System >
Administrators > Create New > REST API Admin) whose profile has read/write on System; set its
trusted hosts to the Patchyard server.

  status   GET  /api/v2/monitor/system/status            model, version, build
           GET  /api/v2/monitor/system/firmware          the running image and what FortiGuard offers
  update   POST /api/v2/monitor/system/firmware/upgrade  {"source": "fortiguard", "filename": <image id>}:
                the newest patch release of the running major.minor (a new major line is only
                reported, never installed); the firewall reboots by itself
  reboot   POST /api/v2/monitor/system/os/reboot
"""
from __future__ import annotations

import time
from typing import IO, Any

from . import DeviceError, DeviceStatus, Driver, FormField


def _ver(img: dict[str, Any]) -> tuple[int, int, int]:
    return int(img.get("major", 0)), int(img.get("minor", 0)), int(img.get("patch", 0))


class FortiGate(Driver):
    supports_update = True
    supports_reboot = True
    required_credentials = ("token",)
    title = "Fortinet FortiGate"
    help = ("The API key of a REST API administrator (System > Administrators > Create New > REST API Admin) with "
            "read/write on System, trusted hosts set to the Patchyard server. Patchyard installs patch releases of "
            "the running version (e.g. 7.4.3 -> 7.4.5) and only reports a new major line.")
    fields = (FormField("token", "API key", secret=True),)
    poll_s = 15.0

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.http.headers["Authorization"] = f"Bearer {self.creds['token']}"

    def _firmware(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        res = self.http.json("GET", "/api/v2/monitor/system/firmware").get("results") or {}
        return res.get("current") or {}, [a for a in res.get("available") or [] if isinstance(a, dict)]

    def _patch_release(self, cur: dict[str, Any], avail: list[dict[str, Any]]) -> dict[str, Any] | None:
        same_line = [a for a in avail if _ver(a)[:2] == _ver(cur)[:2] and _ver(a) > _ver(cur)]
        return max(same_line, key=_ver) if same_line else None

    def status(self, check: bool = False) -> DeviceStatus:
        st = self.http.json("GET", "/api/v2/monitor/system/status")
        res = st.get("results") or {}
        model = " ".join(x for x in (res.get("model_name"), res.get("model_number")) if x) or str(res.get("model", ""))
        version = str(st.get("version") or "")
        cur, avail = self._firmware()
        target = self._patch_release(cur, avail)
        newer_lines = sorted({f"{a.get('major')}.{a.get('minor')}" for a in avail if _ver(a)[:2] > _ver(cur)[:2]})
        notes = ([f"newer major line offered: {', '.join(newer_lines)} (not installed automatically)"]
                 if newer_lines else [])
        return DeviceStatus(model=model or "FortiGate", version=version or str(cur.get("version", "")),
                            latest=str(target.get("version")) if target else version, update_available=bool(target),
                            notes=notes)

    def update(self, log: IO[str]) -> int:
        cur, avail = self._firmware()
        target = self._patch_release(cur, avail)
        if not target:
            log.write(f"{self.dev.name}: no newer patch release of {cur.get('version', '?')}\n")
            return 0
        log.write(f"{self.dev.name}: {cur.get('version', '?')} -> {target.get('version')} from FortiGuard "
                  "(the firewall reboots by itself)\n")
        log.flush()
        out = self.http.json("POST", "/api/v2/monitor/system/firmware/upgrade",
                             body={"source": "fortiguard", "filename": str(target.get("id", ""))})
        if (out.get("results") or {}).get("status") not in (None, "success"):
            log.write(f"!! upgrade refused: {out.get('results')}\n")
            return 1
        return self._wait_back(log, str(target.get("version", "")))

    def _wait_back(self, log: IO[str], want: str, limit_s: float = 1800) -> int:
        deadline, down = time.monotonic() + limit_s, False
        while time.monotonic() < deadline:
            time.sleep(self.poll_s)
            try:
                version = str(self.http.json("GET", "/api/v2/monitor/system/status").get("version") or "")
            except DeviceError:
                if not down:
                    log.write("(firewall rebooting)\n")
                    log.flush()
                    down = True
                continue
            if down or version == want:
                log.write(f"{self.dev.name}: back, running {version}\n")
                return 0 if version == want else 1
        log.write(f"!! {self.dev.name} did not come back within {int(limit_s / 60)} minutes\n")
        return 1

    def reboot(self, log: IO[str]) -> int:
        log.write(f"{self.dev.name}: rebooting\n")
        try:
            self.http.json("POST", "/api/v2/monitor/system/os/reboot", body={"event_log_message": "Patchyard"})
        except DeviceError as e:
            if "cannot reach" not in str(e):
                log.write(f"!! {e}\n")
                return 1
        return 0
