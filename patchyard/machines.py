"""Per-machine settings: defaults for every machine plus overrides per machine, edited in the
dashboard and stored where Ansible reads them.

Store: <state>/machine-settings.json, a second inventory source (ansible/ansible.cfg lists it
after inventory.ini) in Ansible's YAML/JSON inventory format:

  {"all": {"vars": {<defaults>}, "hosts": {"<host>": {<this machine's settings>}}}}

Ansible ranks it below the inventory's own host_vars/ and group_vars/ files, so a value
written there by hand wins; the dashboard shows such a value as "set in an inventory file".
Only machines that are in inventory.ini are written: a host listed here alone would become
a new (ungrouped) machine that every check tries to reach.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import inventory, policy
from .config import Config

WINDOWS_CATEGORIES = ("Critical Updates", "Security Updates", "Update Rollups", "Updates", "Definition Updates",
                      "Drivers", "Upgrades", "Feature Packs", "Tools")
WINDOWS_DEFAULT = WINDOWS_CATEGORIES[:5]       # what patch.yml installs when nothing is set


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class MachineSetting:
    key: str                      # the inventory variable
    label: str
    kind: str                     # time | int | bool | blackout | manual | packages | list | categories | choice
    help: str = ""
    applies: str = "all"          # all | linux (incl. apt) | apt | windows
    builtin: str = ""             # what happens when nothing is set, in words
    lo: int | None = None
    hi: int | None = None
    choices: tuple[str, ...] = ()
    policy: bool = False          # applied by the auto-update policy (run it again after a change)


SCHEMA: tuple[MachineSetting, ...] = (
    MachineSetting("patchyard_auto_updates", "Patchyard sets up daily automatic updates", "bool",
                   "Off: the machine keeps its own update schedule, and the auto-update policy leaves it alone.",
                   builtin="on", policy=True),
    MachineSetting("patchyard_update_time", "Install updates at", "time",
                   "The machine's own local time, every day.", builtin="the system's schedule (around 06:00 on "
                   "Linux, 03:00 on Windows)", policy=True),
    MachineSetting("patchyard_update_spread_minutes", "Start within (minutes) after that", "int",
                   "A random delay, so machines do not all download at once.", "linux", "30 minutes", 0, 240,
                   policy=True),
    MachineSetting("patchyard_reboot_blackout", "No reboots", "blackout",
                   "Patchyard does not reboot the machine in this window (its local time) unless you override "
                   "it when you reboot it yourself.", builtin="no window"),
    MachineSetting("patchyard_manual_only", "Patch only when picked by name", "manual",
                   "Left out when you patch everything; patched when you pick this machine.", builtin="off"),
    MachineSetting("patchyard_hold_packages", "Never update these packages automatically", "packages",
                   "Package name patterns, e.g. postgresql- (automatic updates skip them; Patch does not).",
                   "apt", "none", policy=True),
    MachineSetting("patchyard_track_containers", "Alert when a container stops", "bool", applies="linux", builtin="on"),
    MachineSetting("patchyard_windows_update_categories", "Patch installs", "categories",
                   "Windows Update categories. Upgrades are feature upgrades (e.g. 24H2 to 25H2).", "windows",
                   ", ".join(WINDOWS_DEFAULT), choices=WINDOWS_CATEGORIES),
    MachineSetting("patchyard_windows_update_mode", "Windows Update by itself", "choice",
                   "Download only: updates wait for Patch, so Windows never restarts on its own.", "windows",
                   "install", choices=("install", "download_only"), policy=True),
    MachineSetting("patchyard_auto_update_extra_origins", "Extra sources for automatic updates", "list",
                   'unattended-upgrades origins, one per line, e.g. origin=Docker,codename=${distro_codename}',
                   "apt", "none", policy=True),
    MachineSetting("patchyard_release_upgrade_reenable", "Re-enable after a release upgrade", "list",
                   "Third-party apt sources the upgrade switches off, by part of their name, e.g. docker",
                   "apt", "none"),
)
BY_KEY = {s.key: s for s in SCHEMA}


# ------------------------------------------------------------------ values
def check(key: str, value: Any) -> Any:
    """`value` for setting `key`, normalized; SettingError when it is not acceptable."""
    st = BY_KEY.get(key)
    if st is None:
        raise SettingError(f"unknown machine setting: {key}")
    k = st.kind
    if k == "time":
        if not isinstance(value, str) or not policy.TIME_RE.match(value):
            raise SettingError(f"{st.label}: a time like 03:00")
        return value
    if k == "int":
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit():
            raise SettingError(f"{st.label}: a whole number")
        n = int(value)
        if not (st.lo or 0) <= n <= (st.hi if st.hi is not None else n):
            raise SettingError(f"{st.label}: {st.lo} to {st.hi}")
        return n
    if k == "bool":
        if not isinstance(value, bool):
            raise SettingError(f"{st.label}: on or off")
        return value
    if k == "blackout":
        try:
            policy.parse_blackout(value)
        except policy.PolicyError as e:
            raise SettingError(str(e).replace(key, st.label)) from None
        if "hours_utc" in value:                       # the older form, kept as written
            out = {"hours_utc": sorted(set(value["hours_utc"]))}
        else:
            out = {"days": [d for d in policy.DAYS if d in value.get("days", policy.DAYS)],
                   "start": value["start"], "end": value["end"]}
        return {**out, "reason": str(value.get("reason", "")).strip()} if value.get("reason") else out
    if k == "manual":
        if value is False:
            return False
        if value is True or (isinstance(value, dict) and isinstance(value.get("reason", ""), str)):
            reason = "" if value is True else value.get("reason", "").strip()
            return {"reason": reason} if reason else True
        raise SettingError(f"{st.label}: on (with an optional reason) or off")
    if k == "packages":
        pkgs = value.get("packages") if isinstance(value, dict) else None
        if not isinstance(pkgs, list) or not all(isinstance(p, str) for p in pkgs):
            raise SettingError(f"{st.label}: a list of package names")
        pkgs = [p.strip() for p in pkgs if p.strip()]
        if not pkgs:
            raise SettingError(f"{st.label}: name at least one package (or use the default)")
        reason = str(value.get("reason", "")).strip()
        return {"packages": pkgs, "reason": reason} if reason else {"packages": pkgs}
    if k in ("list", "categories"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingError(f"{st.label}: a list")
        value = [v.strip() for v in value if v.strip()]
        bad = [v for v in value if st.choices and v not in st.choices]
        if bad or (k == "categories" and not value):
            raise SettingError(f"{st.label}: pick at least one of {', '.join(st.choices)}" if not bad
                               else f"{st.label}: unknown {', '.join(bad)}")
        return value
    if k == "choice":
        if value not in st.choices:
            raise SettingError(f"{st.label}: one of {', '.join(st.choices)}")
        return value
    raise SettingError(f"{st.label}: unsupported kind {k}")   # pragma: no cover


# ------------------------------------------------------------------ store
def path(cfg: Config) -> Path:
    return cfg.state / "machine-settings.json"


def ensure(cfg: Config) -> None:
    """Create an empty store if there is none: Ansible warns about a missing inventory source."""
    if not path(cfg).exists():
        _write(cfg, {}, {})


def read(cfg: Config) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """(defaults, {host: settings}); a missing or broken file reads as empty."""
    try:
        data = json.loads(path(cfg).read_text())
        top = data["all"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}
    defaults = top.get("vars") or {}
    hosts = {h: (v or {}) for h, v in (top.get("hosts") or {}).items()}
    return dict(defaults), hosts


def _write(cfg: Config, defaults: dict[str, Any], hosts: dict[str, dict[str, Any]]) -> None:
    known = set(inventory.all_hosts(cfg.inventory_file)) if cfg.inventory_file.exists() else set()
    hosts = {h: v for h, v in sorted(hosts.items()) if v and h in known}
    data = {"all": {"vars": dict(sorted(defaults.items())), "hosts": hosts}}
    tmp = path(cfg).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.chmod(tmp, 0o640)
    tmp.replace(path(cfg))


def update(cfg: Config, host: str | None, values: dict[str, Any]) -> dict[str, list[Any]]:
    """Set (value) or clear (None) settings for `host`, or the defaults when host is None.
    Returns {key: [old, new]} for what changed."""
    defaults, hosts = read(cfg)
    if host is not None and host not in inventory.all_hosts(cfg.inventory_file):
        raise SettingError(f"{host} is not in the inventory")
    target = defaults if host is None else hosts.setdefault(host, {})
    diff: dict[str, list[Any]] = {}
    for key, value in values.items():
        new = None if value is None else check(key, value)
        old = target.get(key)
        if new == old:
            continue
        if new is None:
            target.pop(key, None)
        else:
            target[key] = new
        diff[key] = [old, new]
    if diff:
        _write(cfg, defaults, hosts)
    return diff


def forget(cfg: Config, host: str) -> None:
    """Drop a machine's settings (it left the inventory)."""
    defaults, hosts = read(cfg)
    if hosts.pop(host, None) is not None:
        _write(cfg, defaults, hosts)


# ------------------------------------------------------------------ what Ansible sees
def effective(cfg: Config, host: str) -> dict[str, Any] | None:
    """The machine's settings as Ansible resolves them (all inventory files merged), or None
    when ansible-inventory cannot be run."""
    ansible = cfg.code / "ansible"
    exe = ansible / ".venv" / "bin" / "ansible-inventory"
    found = str(exe) if exe.exists() else shutil.which("ansible-inventory")
    if not found:
        return None
    try:
        r = subprocess.run([found, "--host", host], cwd=ansible, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PATCHYARD_STATE=str(cfg.state)))
        hv = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return {k: v for k, v in hv.items() if k in BY_KEY} if isinstance(hv, dict) else None


def family(os_name: str) -> str:
    """windows | apt | linux | unknown, from the OS name a check reported."""
    o = (os_name or "").lower()
    if not o:
        return "unknown"
    if "windows" in o:
        return "windows"
    if o.startswith(("ubuntu", "debian", "raspbian", "linux mint", "linuxmint", "pop!_os", "kali", "elementary")):
        return "apt"
    return "linux"


def applies(st: MachineSetting, fam: str) -> bool:
    return (fam == "unknown" or st.applies == "all" or st.applies == fam
            or (st.applies == "linux" and fam in ("linux", "apt")))


def view(cfg: Config, host: str | None = None, os_name: str = "") -> dict[str, Any]:
    """Everything the dashboard needs to show and edit settings for a machine (or the defaults)."""
    defaults, hosts = read(cfg)
    schema = [{"key": s.key, "label": s.label, "kind": s.kind, "help": s.help, "applies": s.applies,
               "builtin": s.builtin, "lo": s.lo, "hi": s.hi, "choices": list(s.choices), "policy": s.policy}
              for s in SCHEMA]
    out: dict[str, Any] = {"schema": schema, "defaults": defaults, "days": list(policy.DAYS),
                           "windows_default": list(WINDOWS_DEFAULT)}
    if host is None:
        return out
    mine = hosts.get(host, {})
    eff = effective(cfg, host)
    source = {}
    for s in SCHEMA:
        if eff is None:                                   # Ansible not available: what Patchyard stores
            source[s.key] = "machine" if s.key in mine else "default" if s.key in defaults else "builtin"
        elif s.key not in eff:
            source[s.key] = "builtin"
        elif s.key in mine and eff[s.key] == mine[s.key]:
            source[s.key] = "machine"
        elif s.key not in mine and s.key in defaults and eff[s.key] == defaults[s.key]:
            source[s.key] = "default"
        else:
            source[s.key] = "inventory"                   # host_vars/ or group_vars/ written by hand
    fam = family(os_name)
    out.update({"host": host, "values": mine, "family": fam, "source": source,
                "effective": eff if eff is not None else {**defaults, **mine},
                "applies": [s.key for s in SCHEMA if applies(s, fam)]})
    return out
