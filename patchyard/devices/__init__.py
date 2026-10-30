"""Network devices patched through their own APIs (config `[[devices]]`).

A driver knows one vendor's API. Every driver answers status() (model, version,
newest available version, whether an update waits) and may support update() and
reboot(); the dashboard offers only what a driver supports.

Credentials come from the device's credentials_file (JSON, readable by the service
account only) and are never logged.
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from ..config import Device


class DeviceError(Exception):
    """The device could not be reached or refused us; the message is safe to show."""


class DeviceAuthError(DeviceError):
    """The device refused the credentials. Retrying would risk a captcha or lockout, so the
    check does not try again until the credentials file changes."""


@dataclass
class DeviceStatus:
    model: str = ""
    version: str = ""
    latest: str | None = None              # newest available version, if the device can tell
    update_available: bool | None = None   # None: the device cannot check for updates itself
    needs_reboot: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Http:
    """Small HTTP client: one cookie jar per device session, per-device TLS settings."""

    def __init__(self, dev: Device, timeout: float = 20):
        self.base = dev.url.rstrip("/")
        self.timeout = timeout
        if dev.ca_file:
            ctx = ssl.create_default_context(cafile=dev.ca_file)
        elif dev.verify_tls:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies),
                                                  urllib.request.HTTPSHandler(context=ctx))
        self.headers: dict[str, str] = {}

    def basic_auth(self, user: str, password: str) -> None:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers["Authorization"] = f"Basic {token}"

    def request(self, method: str, path: str, *, form: dict[str, str] | None = None, body: Any = None,
                headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        data = None
        hdrs = {**self.headers, **(headers or {})}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()
        except (urllib.error.URLError, OSError) as e:
            raise DeviceError(f"cannot reach {self.base}: {getattr(e, 'reason', e)}") from None

    def json(self, method: str, path: str, **kw: Any) -> Any:
        code, raw = self.request(method, path, **kw)
        if code == 401:
            raise DeviceAuthError(f"{path}: credentials refused (HTTP 401)")
        if code == 403:
            raise DeviceError(f"{path}: access denied (HTTP 403); the account lacks a privilege")
        if code >= 400:
            raise DeviceError(f"{path}: HTTP {code}")
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise DeviceError(f"{path}: not a JSON reply") from None


@dataclass(frozen=True)
class FormField:
    """One thing the dashboard asks for when a device is added: a credential or an option."""
    key: str
    label: str
    secret: bool = False           # write-only: never sent back to the browser
    option: bool = False           # goes in the device's options, not its credentials file
    required: bool = True
    default: str = ""
    help: str = ""
    choices: tuple[str, ...] = ()


USER = FormField("username", "User name")
PASSWORD = FormField("password", "Password", secret=True)


class Driver:
    """Base class. Subclasses set the flags and implement status(), and optionally update()/reboot()."""
    supports_update = False
    supports_reboot = False
    required_credentials: tuple[str, ...] = ()
    title = ""                     # shown when a device is added in the dashboard
    help = ""                      # which account or key to use, and where to get it
    fields: tuple[FormField, ...] = (USER, PASSWORD)
    web_ok = True                  # may be added from the dashboard (exec may not: it runs commands)

    def __init__(self, dev: Device, creds: dict[str, Any], http: Http | None = None):
        missing = [k for k in self.required_credentials if not creds.get(k)]
        if missing:
            raise DeviceError(f"{dev.credentials_file}: missing {', '.join(missing)}")
        self.dev = dev
        self.creds = creds
        self.http = http or Http(dev)

    def status(self, check: bool = False) -> DeviceStatus:
        """Current state. check=True asks the device to look for new firmware first (slower)."""
        raise NotImplementedError

    def update(self, log: IO[str]) -> int:
        raise DeviceError("this driver cannot update the device")

    def reboot(self, log: IO[str]) -> int:
        raise DeviceError("this driver cannot reboot the device")


def load_credentials(dev: Device) -> dict[str, Any]:
    try:
        data = json.loads(Path(dev.credentials_file).read_text())
    except FileNotFoundError:
        raise DeviceError(f"{dev.credentials_file}: not found") from None
    except PermissionError:
        raise DeviceError(f"{dev.credentials_file}: not readable by the service account") from None
    except ValueError:
        raise DeviceError(f"{dev.credentials_file}: not valid JSON") from None
    if not isinstance(data, dict):
        raise DeviceError(f"{dev.credentials_file}: expected a JSON object")
    return data


def driver_classes() -> dict[str, type[Driver]]:
    from . import asuswrt, fortigate, homeassistant, mikrotik, omada_eap, openwrt, opnsense, synology, truenas, unifi
    from . import exec as exec_
    return {"opnsense": opnsense.OPNsense, "fortigate": fortigate.FortiGate, "asuswrt": asuswrt.AsusWrt,
            "omada_eap": omada_eap.OmadaEAP, "mikrotik": mikrotik.MikroTik, "truenas": truenas.TrueNAS,
            "synology": synology.Synology, "unifi": unifi.UniFi, "openwrt": openwrt.OpenWrt,
            "homeassistant": homeassistant.HomeAssistant, "exec": exec_.Exec}


def driver_for(dev: Device, http: Http | None = None) -> Driver:
    creds = load_credentials(dev) if dev.credentials_file else {}
    return driver_classes()[dev.driver](dev, creds, http)


def capabilities(dev: Device) -> dict[str, bool]:
    cls = driver_classes()[dev.driver]
    if dev.driver == "exec":                   # only what the site configured
        return {"update": bool(dev.options.get("update_command")), "reboot": bool(dev.options.get("reboot_command"))}
    return {"update": cls.supports_update, "reboot": cls.supports_reboot}
