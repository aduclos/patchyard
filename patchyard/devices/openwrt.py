"""OpenWrt, through ubus over HTTP (uhttpd + rpcd, the interface LuCI uses).

Credentials: {"username": "root", "password": "..."} (or an rpcd login with read access).
OpenWrt has no built-in "is there a new release" check, so this driver reports model and
release (update_available = None) and can reboot.

  login    ubus call session login {username, password}   -> ubus_rpc_session
  status   ubus call system board                         -> model, release.description
  reboot   ubus call system reboot
"""
from __future__ import annotations

from typing import IO, Any

from . import PASSWORD, DeviceAuthError, DeviceError, DeviceStatus, Driver, FormField

NULL_SESSION = "00000000000000000000000000000000"


class OpenWrt(Driver):
    supports_update = False
    supports_reboot = True
    required_credentials = ("username", "password")
    title = "OpenWrt router"
    help = "root, or an rpcd login with read access (LuCI's ubus interface). Shows the release and can reboot."
    fields = (FormField("username", "User name", default="root"), PASSWORD)

    def _ubus(self, session: str, obj: str, method: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = self.http.json("POST", "/ubus", body={"jsonrpc": "2.0", "id": 1, "method": "call",
                                                      "params": [session, obj, method, args or {}]})
        result = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(result, list) or not result or result[0] != 0:
            code = result[0] if isinstance(result, list) and result else reply.get("error")
            if obj == "session":
                raise DeviceAuthError("login refused (wrong credentials)")
            raise DeviceError(f"ubus {obj}.{method}: status {code}")
        return result[1] if len(result) > 1 else {}

    def _login(self) -> str:
        data = self._ubus(NULL_SESSION, "session", "login",
                          {"username": self.creds["username"], "password": self.creds["password"]})
        return str(data["ubus_rpc_session"])

    def status(self, check: bool = False) -> DeviceStatus:
        board = self._ubus(self._login(), "system", "board")
        rel = board.get("release") or {}
        version = str(rel.get("description") or rel.get("version") or "")
        return DeviceStatus(model=str(board.get("model") or board.get("board_name") or "OpenWrt"), version=version,
                            latest=None, update_available=None,
                            notes=["OpenWrt has no built-in update check; see the release announcements"])

    def reboot(self, log: IO[str]) -> int:
        log.write(f"{self.dev.name}: rebooting\n")
        self._ubus(self._login(), "system", "reboot")
        return 0
