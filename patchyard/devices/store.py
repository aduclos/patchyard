"""Network devices added in the dashboard.

<state>/devices.json holds them with the same keys as a [[devices]] table; each one's
credentials live in <state>/secrets/device-<name>.json, readable by the service account only,
and never go back to the browser. Devices in the configuration file are listed read-only, and
the exec driver (it runs commands) can only be set up there.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import Config, Device
from . import DeviceError, driver_classes

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class StoreError(ValueError):
    pass


def read(cfg: Config) -> list[dict[str, Any]]:
    try:
        raw = json.loads(cfg.devices_file.read_text())
    except (OSError, ValueError):
        return []
    return [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []


def _write(cfg: Config, items: list[dict[str, Any]]) -> None:
    tmp = cfg.devices_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(items, key=lambda d: d["name"]), indent=1) + "\n")
    os.chmod(tmp, 0o640)
    tmp.replace(cfg.devices_file)


def creds_path(cfg: Config, name: str) -> Path:
    return cfg.state / "secrets" / f"device-{name}.json"


def _write_creds(path: Path, creds: dict[str, str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(creds, fh)
    tmp.replace(path)


def _stored_creds(cfg: Config, name: str | None) -> dict[str, str]:
    if not name:
        return {}
    try:
        data = json.loads(creds_path(cfg, name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ what the dashboard shows
def view(cfg: Config) -> dict[str, Any]:
    mine = {d["name"] for d in read(cfg)}
    drivers = {}
    for name, cls in driver_classes().items():
        if cls.web_ok:
            drivers[name] = {"title": cls.title, "help": cls.help, "update": cls.supports_update,
                             "reboot": cls.supports_reboot, "fields": [asdict(f) for f in cls.fields]}
    devs = []
    for d in cfg.devices:
        cls = driver_classes()[d.driver]
        options = {f.key: d.options.get(f.key, "") for f in cls.fields if f.option}
        stored = _stored_creds(cfg, d.name) if d.name in mine else {}
        # a secret only says whether one is stored; a user name may be shown
        login = {f.key: bool(stored.get(f.key)) if f.secret else stored.get(f.key, "")
                 for f in cls.fields if not f.option} if d.name in mine else {}
        devs.append({"name": d.name, "driver": d.driver, "title": cls.title, "url": d.url,
                     "verify_tls": d.verify_tls, "disruption": d.disruption, "options": options,
                     "source": "dashboard" if d.name in mine else "file", "credentials": login})
    return {"drivers": drivers, "devices": devs}


# ------------------------------------------------------------------ checking a form
def check(cfg: Config, form: dict[str, Any], creds: dict[str, Any] | None,
          original: str | None = None) -> tuple[Device, dict[str, str]]:
    """The device and its complete credentials from a dashboard form. Secrets left empty keep
    the stored value when an existing device is edited. StoreError when something is wrong."""
    if not isinstance(form, dict):
        raise StoreError("expected a device")
    name, driver = str(form.get("name", "")).strip().lower(), str(form.get("driver", ""))
    cls = driver_classes().get(driver)
    if cls is None or not cls.web_ok:
        raise StoreError("pick a device type" if cls is None else "this type is set up in the configuration file only")
    if not NAME_RE.match(name):
        raise StoreError("Name: lower-case letters, digits, - and _ (up to 40)")
    file_names = {d.name for d in cfg.devices} - {d["name"] for d in read(cfg)}
    if name in file_names:
        raise StoreError(f"{name} is defined in the configuration file")
    if name != original and name in {d["name"] for d in read(cfg)}:
        raise StoreError(f"there is already a device called {name}")
    url = str(form.get("url", "")).strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", url):
        raise StoreError("Address: the device's web address, e.g. https://192.0.2.1")
    options: dict[str, str] = {}
    for f in cls.fields:
        if f.option:
            v = str((form.get("options") or {}).get(f.key, "") or f.default).strip()
            if f.required and not v:
                raise StoreError(f"{f.label} is required")
            if f.choices and v and v not in f.choices:
                raise StoreError(f"{f.label}: one of {', '.join(f.choices)}")
            if v:
                options[f.key] = v
    have = _stored_creds(cfg, original)
    full: dict[str, str] = {}
    for f in cls.fields:
        if f.option:
            continue
        v = str((creds or {}).get(f.key) or "")
        full[f.key] = (v if f.secret else v.strip()) or have.get(f.key, "") or f.default
        if f.required and not full[f.key]:
            raise StoreError(f"{f.label} is required")
    dev = Device(name=name, driver=driver, url=url, credentials_file=str(creds_path(cfg, name)),
                 verify_tls=bool(form.get("verify_tls", True)), disruption=str(form.get("disruption", "")).strip(),
                 options=options)
    return dev, full


def save(cfg: Config, form: dict[str, Any], creds: dict[str, Any] | None,
         original: str | None = None) -> dict[str, Any]:
    """Add or change a device. Returns what changed for the audit log (secrets as "(changed)")."""
    dev, full = check(cfg, form, creds, original)
    items = [d for d in read(cfg) if d["name"] not in (original, dev.name)]
    before = next((d for d in read(cfg) if d["name"] == (original or dev.name)), None)
    entry = {"name": dev.name, "driver": dev.driver, "url": dev.url, "credentials_file": dev.credentials_file,
             "verify_tls": dev.verify_tls, "disruption": dev.disruption, "options": dev.options}
    if original and original != dev.name:
        creds_path(cfg, original).unlink(missing_ok=True)
    _write_creds(creds_path(cfg, dev.name), full)
    _write(cfg, [*items, entry])
    changes: dict[str, Any] = {k: [before.get(k) if before else None, v] for k, v in entry.items()
                               if k != "credentials_file" and (not before or before.get(k) != v)}
    cls = driver_classes()[dev.driver]
    typed = [f.key for f in cls.fields if not f.option and str((creds or {}).get(f.key, "") or "")]
    if typed:
        changes["login"] = "(changed)"
    return changes


def remove(cfg: Config, name: str) -> None:
    items = read(cfg)
    if name not in {d["name"] for d in items}:
        raise StoreError(f"{name} is not a device added in the dashboard")
    _write(cfg, [d for d in items if d["name"] != name])
    creds_path(cfg, name).unlink(missing_ok=True)


def test(cfg: Config, form: dict[str, Any], creds: dict[str, Any] | None,
         original: str | None = None) -> dict[str, Any]:
    """Sign in once with what the form says and read the device's status; nothing is saved."""
    dev, full = check(cfg, form, creds, original)
    try:
        st = driver_classes()[dev.driver](dev, full).status(check=False)
    except DeviceError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:                    # a driver bug must reach the person, not crash the request
        return {"ok": False, "error": f"driver error: {e}"}
    return {"ok": True, **st.to_dict()}
