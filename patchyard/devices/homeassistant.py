"""Home Assistant, through its REST API: Core, the operating system, the Supervisor, add-ons
and device firmware, all of which it exposes as update entities.

Credentials: {"token": "..."}, a long-lived access token (your profile > Security >
Long-lived access tokens), ideally of a dedicated administrator user.

  status   GET  /api/config                     version, installation name
           GET  /api/states                     update.* entities: state "on" = an update waits,
                                                attributes installed_version / latest_version / title
  update   POST /api/services/update/install    each waiting update in turn (others, then Core, then
                                                the OS last), with a backup where it is supported;
                                                waits for each to finish (Core restarts itself)
  reboot   POST /api/services/hassio/host_reboot   on Home Assistant OS / Supervised
           POST /api/services/homeassistant/restart  otherwise (Core in a container)
"""
from __future__ import annotations

import time
from typing import IO, Any

from . import DeviceError, DeviceStatus, Driver, FormField

CORE = "update.home_assistant_core_update"
OS = "update.home_assistant_operating_system_update"
SUPERVISOR = "update.home_assistant_supervisor_update"
BACKUP_FEATURE = 8                    # UpdateEntityFeature.BACKUP


class HomeAssistant(Driver):
    supports_update = True
    supports_reboot = True
    required_credentials = ("token",)
    title = "Home Assistant"
    help = ("A long-lived access token: your profile in Home Assistant > Security > Long-lived access tokens > "
            "Create token. An administrator's token is needed to install updates. The address is the one you open "
            "Home Assistant with, e.g. http://192.0.2.10:8123.")
    fields = (FormField("token", "Long-lived access token", secret=True),)
    poll_s = 10.0

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.http.headers["Authorization"] = f"Bearer {self.creds['token']}"

    def _updates(self) -> list[dict[str, Any]]:
        states = self.http.json("GET", "/api/states")
        return [s for s in states if isinstance(s, dict) and str(s.get("entity_id", "")).startswith("update.")]

    @staticmethod
    def _label(u: dict[str, Any]) -> str:
        a = u.get("attributes") or {}
        return str(a.get("title") or a.get("friendly_name") or u["entity_id"])

    def status(self, check: bool = False) -> DeviceStatus:
        cfg = self.http.json("GET", "/api/config")
        updates = self._updates()
        waiting = [u for u in updates if u.get("state") == "on"]
        version = str(cfg.get("version", ""))
        core = next((u for u in updates if u["entity_id"] == CORE), None)
        latest = str((core.get("attributes") or {}).get("latest_version") or version) if core else version
        kind = "OS" if any(u["entity_id"] == OS for u in updates) else ""
        notes = [f"{self._label(u)}: {(u.get('attributes') or {}).get('installed_version', '?')} -> "
                 f"{(u.get('attributes') or {}).get('latest_version', '?')}" for u in waiting]
        return DeviceStatus(model=f"Home Assistant {kind}".strip(), version=version,
                            latest=latest if not waiting or core in waiting else f"{len(waiting)} update(s)",
                            update_available=bool(waiting), notes=notes)

    def _wait_done(self, log: IO[str], entity: str, limit_s: float) -> bool:
        """Until the entity no longer reports an update in progress (Home Assistant may restart)."""
        deadline, down = time.monotonic() + limit_s, False
        while time.monotonic() < deadline:
            time.sleep(self.poll_s)
            try:
                st = self.http.json("GET", f"/api/states/{entity}")
            except DeviceError:
                if not down:
                    log.write("  (Home Assistant is restarting)\n")
                    log.flush()
                    down = True
                continue
            a = st.get("attributes") or {}
            if not a.get("in_progress"):
                ok = st.get("state") == "off"
                log.write(f"  {'done' if ok else 'still offered after the install'}: "
                          f"{a.get('installed_version', '?')}\n")
                log.flush()
                return ok
        log.write("  timed out waiting for it\n")
        return False

    def update(self, log: IO[str]) -> int:
        waiting = [u for u in self._updates() if u.get("state") == "on"]
        if not waiting:
            log.write(f"{self.dev.name}: nothing to install\n")
            return 0
        order = {CORE: 1, SUPERVISOR: 1, OS: 2}            # everything else first; the OS (a reboot) last
        rc = 0
        for u in sorted(waiting, key=lambda u: order.get(u["entity_id"], 0)):
            a = u.get("attributes") or {}
            body: dict[str, Any] = {"entity_id": u["entity_id"]}
            if int(a.get("supported_features") or 0) & BACKUP_FEATURE:
                body["backup"] = True
            log.write(f"{self.dev.name}: installing {self._label(u)} {a.get('installed_version', '?')} -> "
                      f"{a.get('latest_version', '?')}{' (backup first)' if body.get('backup') else ''}\n")
            log.flush()
            try:
                self.http.json("POST", "/api/services/update/install", body=body)
            except DeviceError as e:
                if u["entity_id"] not in (CORE, OS):        # Core/OS cut the connection when they restart
                    log.write(f"  !! {e}\n")
                    rc = 1
                    continue
            if not self._wait_done(log, u["entity_id"], 3600 if u["entity_id"] == OS else 1800):
                rc = 1
        return rc

    def reboot(self, log: IO[str]) -> int:
        host = any(u["entity_id"] == OS for u in self._updates())
        path = "/api/services/hassio/host_reboot" if host else "/api/services/homeassistant/restart"
        log.write(f"{self.dev.name}: {'rebooting the host' if host else 'restarting Home Assistant'}\n")
        try:
            self.http.json("POST", path, body={})
        except DeviceError as e:
            if "HTTP 5" not in str(e) and "cannot reach" not in str(e):   # a cut connection is the reboot
                log.write(f"!! {e}\n")
                return 1
        return 0
