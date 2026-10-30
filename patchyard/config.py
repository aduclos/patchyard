"""Site configuration: one TOML file, validated against a typed schema.

Precedence (lowest to highest):
  1. defaults in the dataclasses below
  2. the site file (PATCHYARD_CONFIG, default /etc/patchyard/patchyard.toml)
  3. runtime overrides saved from the web UI (<state>/settings.json), limited to
     the keys in SETTINGS and not listed in web.settings_locked

Unknown keys and wrong types are errors, so a typo fails loudly at start-up
instead of being silently ignored. Nothing in the code base may hard-code a
host, domain, address or realm: those all come from here or from the
inventory (per-host `patchyard_*` variables, see patchyard/policy.py).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

DEFAULT_CONFIG = "/etc/patchyard/patchyard.toml"
log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """The site configuration is invalid."""


@dataclass
class Site:
    name: str = "Patchyard"
    url: str = ""                  # public URL of the dashboard; used for the Origin check and links
    domain: str = ""               # appended to short host names on enrollment, e.g. ".corp.example.com"


@dataclass
class Paths:
    code: str = "/srv/patchyard"
    state: str = "/var/lib/patchyard"


@dataclass
class ServiceAccount:
    name: str = "patchyard"
    key: str = "~/.ssh/patchyard_ed25519"
    # Source addresses the key is accepted from on managed machines (authorized_keys
    # from=). Empty: the controller's own IPv4 addresses, detected at enrollment.
    allowed_sources: list[str] = field(default_factory=list)
    # "inet" (IPv4) keeps allowed_sources meaningful when IPv6 privacy addresses rotate.
    address_family: str = "inet"


@dataclass
class Inventory:
    managed_group: str = "managed"        # machines the tool patches, reboots, enrolls into
    report_only_group: str = "report_only"  # shown on the dashboard, never changed


@dataclass
class Web:
    bind: str = "127.0.0.1"
    port: int = 8790
    session_hours: int = 8
    serve_static: bool = True      # serve the web app itself (no separate web server needed)
    settings_locked: list[str] = field(default_factory=list)   # keys/sections the UI may not change; "*" = all


@dataclass
class Auth:
    method: str = "local"          # local | kerberos | none
    realm: str = ""                # Kerberos realm, e.g. CORP.EXAMPLE.COM (kerberos only)
    group: str = "patchyard-admins"  # kerberos: members of this group may sign in (resolved via NSS)
    max_failures: int = 5
    failure_window_minutes: int = 15
    lockout_minutes: int = 15


@dataclass
class Checks:
    interval_minutes: int = 15
    stale_auto_update_hours: int = 48
    container_memory_days: int = 3
    release_upgrade_idle_minutes: int = 15
    digest_hour: int = 9           # local hour of the daily "still waiting on you" reminder; -1 = off
    device_firmware_check_hours: int = 24   # how often devices are asked to look for new firmware


@dataclass
class Ntfy:
    url: str = ""                  # e.g. https://ntfy.sh or http://127.0.0.1:2586
    topic: str = ""
    token_file: str = ""           # optional; read at send time, never logged


@dataclass
class Webhook:
    url: str = ""                  # receives JSON {"title", "body", "priority", "url"}


@dataclass
class Smtp:
    host: str = ""
    port: int = 587
    starttls: bool = True
    user: str = ""
    password_file: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)


@dataclass
class Notify:
    backends: list[str] = field(default_factory=list)   # any of: ntfy, webhook, smtp
    ntfy: Ntfy = field(default_factory=Ntfy)
    webhook: Webhook = field(default_factory=Webhook)
    smtp: Smtp = field(default_factory=Smtp)


@dataclass
class Controller:
    # Inventory name of the machine running this tool. It is a machine like the others,
    # with two differences: it is never rebooted in the middle of a job (only on its own,
    # with reboot_command, after a typed confirmation), and it can be patched with
    # patch_command instead of the playbook, so the service account needs no root on it:
    # one narrow sudo rule per command. With patch_command it is patchable even when its
    # inventory group is report-only, and the auto-update policy leaves it alone.
    host: str = ""
    reboot_command: list[str] = field(default_factory=list)
    patch_command: list[str] = field(default_factory=list)
    # Optional executable printing a JSON object of extra status for the controller
    # (e.g. its own patch tooling); merged into its row on the dashboard.
    status_plugin: str = ""


@dataclass
class Device:
    """A network device patched through its own API (router, access point, ...)."""
    name: str = ""                 # shown on the dashboard; unique
    driver: str = ""               # see DEVICE_DRIVERS and patchyard/devices/
    url: str = ""                  # base URL of its web/API interface, e.g. https://192.0.2.1
    credentials_file: str = ""     # JSON, readable by the service account only (see patchyard/devices/)
    verify_tls: bool = True        # False only for self-signed device certificates
    ca_file: str = ""              # or trust a specific CA bundle for this device
    disruption: str = ""           # what an update/reboot interrupts, shown in the confirmation
    options: dict[str, str] = field(default_factory=dict)   # driver-specific settings (see each driver)


@dataclass
class Config:
    site: Site = field(default_factory=Site)
    paths: Paths = field(default_factory=Paths)
    service_account: ServiceAccount = field(default_factory=ServiceAccount)
    inventory: Inventory = field(default_factory=Inventory)
    web: Web = field(default_factory=Web)
    auth: Auth = field(default_factory=Auth)
    checks: Checks = field(default_factory=Checks)
    notify: Notify = field(default_factory=Notify)
    controller: Controller = field(default_factory=Controller)
    devices: list[Device] = field(default_factory=list)

    # ---- derived paths (not configurable separately) ----
    @property
    def state(self) -> Path:
        return Path(self.paths.state)

    @property
    def code(self) -> Path:
        return Path(self.paths.code)

    @property
    def settings_file(self) -> Path:
        return self.state / "settings.json"

    @property
    def devices_file(self) -> Path:
        """Network devices added in the dashboard (patchyard/devices/store.py)."""
        return self.state / "devices.json"

    @property
    def inventory_file(self) -> Path:
        return self.state / "inventory.ini"

    @property
    def ssh_key(self) -> Path:
        return Path(os.path.expanduser(self.service_account.key))


# ------------------------------------------------------------------ settings in the web UI
@dataclass(frozen=True)
class Setting:
    """One setting on the Settings page. Changes are saved to <state>/settings.json."""
    key: str                       # dotted config key
    label: str
    section: str                   # see SECTIONS
    kind: str = "text"             # text | int | bool | hour | list | choices | secret | readonly
    help: str = ""
    lo: int | None = None          # int: allowed range
    hi: int | None = None
    choices: tuple[str, ...] = ()  # choices: the allowed values (several may be picked)
    show_if: str = ""              # "key=value", "key!=value" or "key~item" (a list containing item)


SECTIONS = {"general": "General", "schedule": "Schedule", "notifications": "Notifications", "signin": "Sign-in"}

SETTINGS: tuple[Setting, ...] = (
    Setting("site.name", "Name shown on the dashboard", "general"),
    Setting("site.url", "Dashboard address", "general", "readonly",
            "Every change made here is checked against this address, so a wrong value would lock this page. "
            "Change it in the configuration file."),
    Setting("site.domain", "Domain for short names", "general",
            help="Added when you add a machine by its short name, e.g. .corp.example.com"),
    Setting("checks.interval_minutes", "Check every (minutes)", "schedule", "int",
            "How often every machine and device is checked. Checks only read; they change nothing.", 5, 1440),
    Setting("checks.digest_hour", "Daily reminder", "schedule", "hour",
            "One reminder a day while something still waits on you (local time of the Patchyard server)."),
    Setting("checks.device_firmware_check_hours", "Ask devices for new firmware every (hours)", "schedule", "int",
            lo=1, hi=168),
    Setting("checks.stale_auto_update_hours", "Warn when automatic updates have not run for (hours)", "schedule",
            "int", lo=6, hi=720),
    Setting("checks.container_memory_days", "Alert about a stopped container for (days)", "schedule", "int",
            "A container seen running in this window and now gone is a problem; after it, it is forgotten.", 1, 60),
    Setting("checks.release_upgrade_idle_minutes", "Release upgrade looks stuck after (minutes idle)", "schedule",
            "int", lo=5, hi=240),
    Setting("notify.backends", "Send alerts to", "notifications", "choices",
            "Only new problems and waiting reboots, plus the daily reminder.", choices=("ntfy", "webhook", "smtp")),
    Setting("notify.ntfy.url", "ntfy server", "notifications", help="e.g. https://ntfy.sh",
            show_if="notify.backends~ntfy"),
    Setting("notify.ntfy.topic", "ntfy topic", "notifications", show_if="notify.backends~ntfy"),
    Setting("notify.ntfy.token_file", "ntfy access token", "notifications", "secret",
            "Optional. Stored on the server, readable only by Patchyard; never shown again.",
            show_if="notify.backends~ntfy"),
    Setting("notify.webhook.url", "Webhook URL", "notifications",
            help='Receives a JSON POST: {"title", "body", "priority", "url"}', show_if="notify.backends~webhook"),
    Setting("notify.smtp.host", "Mail server", "notifications", show_if="notify.backends~smtp"),
    Setting("notify.smtp.port", "Port", "notifications", "int", lo=1, hi=65535, show_if="notify.backends~smtp"),
    Setting("notify.smtp.starttls", "Use STARTTLS", "notifications", "bool", show_if="notify.backends~smtp"),
    Setting("notify.smtp.user", "User name", "notifications", help="Empty: send without signing in.",
            show_if="notify.backends~smtp"),
    Setting("notify.smtp.password_file", "Password", "notifications", "secret",
            "Stored on the server, readable only by Patchyard; never shown again.", show_if="notify.backends~smtp"),
    Setting("notify.smtp.sender", "From address", "notifications", show_if="notify.backends~smtp"),
    Setting("notify.smtp.recipients", "Send to", "notifications", "list", "One address per line.",
            show_if="notify.backends~smtp"),
    Setting("auth.method", "Sign-in method", "signin", "readonly",
            "Changing it can lock everyone out, so it is set in the configuration file (or the setup wizard)."),
    Setting("auth.group", "Group allowed to sign in", "signin", show_if="auth.method=kerberos"),
    Setting("auth.max_failures", "Lock sign-in after (failed attempts)", "signin", "int", lo=1, hi=100,
            show_if="auth.method!=none"),
    Setting("auth.failure_window_minutes", "…within (minutes)", "signin", "int", lo=1, hi=1440,
            show_if="auth.method!=none"),
    Setting("auth.lockout_minutes", "Lock for (minutes)", "signin", "int", lo=1, hi=1440,
            show_if="auth.method!=none"),
    Setting("web.session_hours", "Stay signed in for (hours)", "signin", "int", lo=1, hi=720,
            show_if="auth.method!=none"),
)
SETTING = {st.key: st for st in SETTINGS}
EDITABLE = {st.key for st in SETTINGS if st.kind != "readonly"}


def locked(cfg: Config, key: str) -> bool:
    """True when web.settings_locked keeps `key` file-only (an exact key, a prefix such as
    "notify", or "*" for everything)."""
    return any(p == "*" or key == p or key.startswith(p + ".") for p in cfg.web.settings_locked)


def coerce(key: str, value: Any) -> Any:
    """A value from the web UI, checked against its Setting (ConfigError when wrong)."""
    st = SETTING.get(key)
    if st is None or st.kind == "readonly":
        raise ConfigError(f"{key}: not editable from the web UI")
    if st.kind == "int" or st.kind == "hour":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{st.label}: a whole number") from None
        lo, hi = (-1, 23) if st.kind == "hour" else (st.lo, st.hi)
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ConfigError(f"{st.label}: {lo} to {hi}")
        return value
    if st.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{st.label}: true or false")
        return value
    if st.kind in ("list", "choices"):
        if isinstance(value, str):
            value = [v.strip() for v in value.replace(",", "\n").splitlines()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{st.label}: a list")
        value = [v.strip() for v in value if v.strip()]
        bad = [v for v in value if st.choices and v not in st.choices]
        if bad:
            raise ConfigError(f"{st.label}: unknown {', '.join(bad)}")
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{st.label}: text")
    return value.strip()


AUTH_METHODS = {"local", "kerberos", "none"}
DEVICE_DRIVERS = {"opnsense", "fortigate", "asuswrt", "omada_eap", "mikrotik", "truenas", "synology", "unifi",
                  "openwrt", "homeassistant", "exec"}
NO_CREDENTIALS = {"exec"}          # drivers that may run without a credentials file
NOTIFY_BACKENDS = {"ntfy", "webhook", "smtp"}


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    """Recursively turn a dict into dataclass `cls`, rejecting unknown keys and bad types."""
    hints = get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    kwargs = {}
    for name, value in data.items():
        typ = hints[name]
        path = f"{where}.{name}" if where else name
        if dataclasses.is_dataclass(typ):
            if not isinstance(value, dict):
                raise ConfigError(f"{path}: expected a table")
            kwargs[name] = _build(typ, value, path)
        elif getattr(typ, "__origin__", None) is list and dataclasses.is_dataclass(typ.__args__[0]):
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                raise ConfigError(f"{path}: expected a list of tables ([[{path}]])")
            kwargs[name] = [_build(typ.__args__[0], v, f"{path}[{i}]") for i, v in enumerate(value)]
        else:
            kwargs[name] = _check_type(typ, value, path)
    return cls(**kwargs)


def _check_type(typ: Any, value: Any, path: str) -> Any:
    origin = getattr(typ, "__origin__", None)
    if origin is dict:
        key_t, val_t = typ.__args__
        if not isinstance(value, dict) or not all(
                isinstance(k, key_t) and isinstance(v, val_t) for k, v in value.items()):
            raise ConfigError(f"{path}: expected a table of {val_t.__name__} values")
        return dict(value)
    if origin is list:
        (item,) = typ.__args__
        if not isinstance(value, list) or not all(isinstance(v, item) for v in value):
            raise ConfigError(f"{path}: expected a list of {item.__name__}")
        return value
    if typ is bool:
        ok = isinstance(value, bool)
    elif typ is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, typ)
    if not ok:
        raise ConfigError(f"{path}: expected {typ.__name__}, got {type(value).__name__}")
    return value


def _merge(base: dict[str, Any], dotted: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for key, value in dotted.items():
        node = out
        *parents, leaf = key.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    return out


def validate(cfg: Config) -> None:
    """Cross-field rules the type system cannot express."""
    if cfg.auth.method not in AUTH_METHODS:
        raise ConfigError(f"auth.method: one of {', '.join(sorted(AUTH_METHODS))}")
    if cfg.auth.method == "kerberos" and not cfg.auth.realm:
        raise ConfigError("auth.realm is required for auth.method = \"kerberos\"")
    bad = set(cfg.notify.backends) - NOTIFY_BACKENDS
    if bad:
        raise ConfigError(f"notify.backends: unknown {', '.join(sorted(bad))}")
    if "ntfy" in cfg.notify.backends and not (cfg.notify.ntfy.url and cfg.notify.ntfy.topic):
        raise ConfigError("notify.ntfy.url and notify.ntfy.topic are required for the ntfy backend")
    if "webhook" in cfg.notify.backends and not cfg.notify.webhook.url:
        raise ConfigError("notify.webhook.url is required for the webhook backend")
    if "smtp" in cfg.notify.backends and not (cfg.notify.smtp.host and cfg.notify.smtp.recipients):
        raise ConfigError("notify.smtp.host and notify.smtp.recipients are required for the smtp backend")
    if not 5 <= cfg.checks.interval_minutes <= 1440:
        raise ConfigError("checks.interval_minutes: 5 to 1440")
    if not -1 <= cfg.checks.digest_hour <= 23:
        raise ConfigError("checks.digest_hour: -1 (off) or 0-23")
    if cfg.service_account.address_family not in {"inet", "inet6", "any"}:
        raise ConfigError("service_account.address_family: inet, inet6 or any")
    names = [d.name for d in cfg.devices]
    if len(names) != len(set(names)):
        raise ConfigError("devices: names must be unique")
    for i, d in enumerate(cfg.devices):
        where = f"devices[{i}] ({d.name or 'unnamed'})"
        if not d.name or not d.driver or (not d.url and d.driver != "exec"):
            raise ConfigError(f"{where}: name, driver and url are required")
        if not d.credentials_file and d.driver not in NO_CREDENTIALS:
            raise ConfigError(f"{where}: credentials_file is required for the {d.driver} driver")
        if d.driver not in DEVICE_DRIVERS:
            raise ConfigError(f"{where}: driver must be one of {', '.join(sorted(DEVICE_DRIVERS))}")
        if d.driver != "exec" and not d.url.startswith(("https://", "http://")):
            raise ConfigError(f"{where}: url must start with https:// or http://")
    if not cfg.site.url.startswith(("https://", "http://")):
        raise ConfigError("site.url: the dashboard's full URL, e.g. https://patch.example.com")


def config_path() -> str:
    """PATCHYARD_CONFIG, else /etc/patchyard/patchyard.toml if it exists, else patchyard.toml in
    the state directory (where the setup wizard writes it)."""
    env = os.environ.get("PATCHYARD_CONFIG")
    if env:
        return env
    if Path(DEFAULT_CONFIG).exists():
        return DEFAULT_CONFIG
    return str(Path(os.environ.get("PATCHYARD_STATE") or Paths().state) / "patchyard.toml")


def load(path: str | os.PathLike[str] | None = None, *, overrides: bool = True) -> Config:
    """Load and validate the site configuration."""
    path = Path(path or config_path())
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found (copy config/patchyard.example.toml)") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    cfg = _build(Config, data, "")
    if overrides:
        saved = read_overrides(cfg)
        if saved:
            cfg = _build(Config, _merge(data, saved), "")
    in_file = {d.name for d in cfg.devices}
    cfg.devices += [d for d in read_ui_devices(cfg) if d.name not in in_file]   # the file wins a name clash
    validate(cfg)
    return cfg


def read_ui_devices(cfg: Config) -> list[Device]:
    """Devices added in the dashboard. An unreadable entry is skipped with a warning instead of
    stopping start-up: the dashboard, where it would be fixed, must still come up."""
    try:
        raw = json.loads(cfg.devices_file.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("%s: %s; ignoring it", cfg.devices_file, e)
        return []
    out = []
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        try:
            if not isinstance(item, dict):
                raise ConfigError("expected an object")
            out.append(_build(Device, item, f"{cfg.devices_file.name}[{i}]"))
        except ConfigError as e:
            log.warning("%s; skipping it", e)
    return out


def read_overrides(cfg: Config) -> dict[str, Any]:
    """Settings saved from the web UI; keys that are not editable or are locked are ignored."""
    try:
        saved = json.loads(cfg.settings_file.read_text())
    except (OSError, ValueError):            # missing or unreadable: no overrides
        return {}
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items() if k in EDITABLE and not locked(cfg, k)}


def save_overrides(cfg: Config, changes: dict[str, Any],
                   path: str | os.PathLike[str] | None = None) -> tuple[Config, dict[str, tuple[Any, Any]]]:
    """Check `changes` (dotted keys from SETTINGS) on top of the current configuration, persist
    them, and return the new configuration plus {key: (old, new)} for what really changed."""
    clean = {}
    for key, value in changes.items():
        if locked(cfg, key):
            raise ConfigError(f"{SETTING[key].label if key in SETTING else key}: set in the configuration file")
        clean[key] = coerce(key, value)
    diff = {k: (get(cfg, k), v) for k, v in clean.items() if get(cfg, k) != v}
    merged = {**read_overrides(cfg), **clean}
    path = Path(path or config_path())
    candidate = _build(Config, _merge(tomllib.loads(path.read_text()), merged), "")
    validate(candidate)
    tmp = cfg.settings_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=1, sort_keys=True))
    os.chmod(tmp, 0o640)
    tmp.replace(cfg.settings_file)
    return candidate, diff


def get(cfg: Config, dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        node = getattr(node, part)
    return node
