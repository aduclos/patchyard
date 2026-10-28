"""Site configuration: one TOML file, validated against a typed schema.

Precedence (lowest to highest):
  1. defaults in the dataclasses below
  2. the site file (FLEET_CONFIG, default /etc/fleet/fleet.toml)
  3. runtime overrides saved from the web UI (<state>/settings.json), limited to
     the keys in EDITABLE

Unknown keys and wrong types are errors, so a typo fails loudly at start-up
instead of being silently ignored. Nothing in the code base may hard-code a
host, domain, address or realm: those all come from here or from the
inventory (per-host `fleet_*` variables, see fleet/policy.py).
"""
from __future__ import annotations

import dataclasses
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

DEFAULT_CONFIG = "/etc/fleet/fleet.toml"


class ConfigError(ValueError):
    """The site configuration is invalid."""


@dataclass
class Site:
    name: str = "Fleet patching"
    url: str = ""                  # public URL of the dashboard; used for the Origin check and links
    domain: str = ""               # appended to short host names on enrollment, e.g. ".corp.example.com"


@dataclass
class Paths:
    code: str = "/srv/fleet"
    state: str = "/var/lib/fleet"


@dataclass
class ServiceAccount:
    name: str = "svc_patch"
    key: str = "~/.ssh/fleet_ed25519"
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


@dataclass
class Auth:
    method: str = "kerberos"       # kerberos | none
    realm: str = ""                # Kerberos realm, e.g. CORP.EXAMPLE.COM (kerberos only)
    group: str = "fleetadmins"     # members of this group may sign in (resolved via NSS)
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
    # Inventory name of the machine running this tool. It is never patched by the
    # tool; it can be rebooted from the UI with a typed confirmation, if
    # reboot_command is set (it must be allowed by a narrow sudo rule).
    host: str = ""
    reboot_command: list[str] = field(default_factory=list)
    # Optional executable printing a JSON object of extra status for the controller
    # (e.g. its own patch tooling); merged into its row on the dashboard.
    status_plugin: str = ""


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
    def inventory_file(self) -> Path:
        return self.state / "inventory.ini"

    @property
    def ssh_key(self) -> Path:
        return Path(os.path.expanduser(self.service_account.key))


# Keys the web UI may change at run time (saved to <state>/settings.json).
EDITABLE: dict[str, str] = {
    "auth.group": "Group allowed to sign in",
    "checks.interval_minutes": "Check interval (minutes)",
    "checks.digest_hour": "Daily reminder hour (-1 = off)",
    "notify.ntfy.topic": "ntfy topic",
}

AUTH_METHODS = {"kerberos", "none"}
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
        else:
            kwargs[name] = _check_type(typ, value, path)
    return cls(**kwargs)


def _check_type(typ: Any, value: Any, path: str) -> Any:
    origin = getattr(typ, "__origin__", None)
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
    if not cfg.site.url.startswith(("https://", "http://")):
        raise ConfigError("site.url: the dashboard's full URL, e.g. https://patch.example.com")


def load(path: str | os.PathLike[str] | None = None, *, overrides: bool = True) -> Config:
    """Load and validate the site configuration."""
    path = Path(path or os.environ.get("FLEET_CONFIG", DEFAULT_CONFIG))
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found (copy config/fleet.example.toml)") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    cfg = _build(Config, data, "")
    if overrides:
        saved = read_overrides(cfg)
        if saved:
            cfg = _build(Config, _merge(data, saved), "")
    validate(cfg)
    return cfg


def read_overrides(cfg: Config) -> dict[str, Any]:
    try:
        saved = json.loads(cfg.settings_file.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return {k: v for k, v in saved.items() if k in EDITABLE}


def save_overrides(cfg: Config, changes: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Config:
    """Validate `changes` (dotted keys from EDITABLE) on top of the current config, then persist."""
    bad = set(changes) - set(EDITABLE)
    if bad:
        raise ConfigError(f"not editable from the UI: {', '.join(sorted(bad))}")
    merged = {**read_overrides(cfg), **changes}
    path = Path(path or os.environ.get("FLEET_CONFIG", DEFAULT_CONFIG))
    candidate = _build(Config, _merge(tomllib.loads(path.read_text()), merged), "")
    validate(candidate)
    tmp = cfg.settings_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=1, sort_keys=True))
    tmp.replace(cfg.settings_file)
    return candidate


def get(cfg: Config, dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        node = getattr(node, part)
    return node
