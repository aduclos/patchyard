"""First-run setup: the wizard's server side.

`python -m patchyard web` without a configuration file starts in setup mode: only the
wizard is served, and every wizard call needs the one-time setup code stored in
<state>/setup-token (the installer prints it). Finishing writes a validated config file,
creates the first local admin (if local sign-in was chosen), removes the setup code and
switches the running server to the normal dashboard.
"""
from __future__ import annotations

import dataclasses
import hmac
import os
import secrets
from pathlib import Path
from typing import Any

from . import config as configmod
from .accounts import Users
from .config import Config, ConfigError

TOKEN_FILE = "setup-token"


def state_dir() -> Path:
    return Path(os.environ.get("PATCHYARD_STATE") or configmod.Paths().state)


def setup_token(state: Path) -> str:
    """The setup code, created (0600) on first use."""
    f = state / TOKEN_FILE
    try:
        return f.read_text().strip()
    except FileNotFoundError:
        token = "-".join(secrets.token_hex(3) for _ in range(3))      # e.g. 3f9a1c-77b20e-c41d55
        state.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (token + "\n").encode())
        os.close(fd)
        return token


def code_ok(state: Path, code: str) -> bool:
    try:
        return hmac.compare_digest(setup_token(state), (code or "").strip().lower())
    except OSError:
        return False


# ------------------------------------------------------------------ TOML
def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def toml_dumps(cfg: Config) -> str:
    """The configuration as TOML (stdlib has a TOML reader only). Every key is written, so
    the file documents itself; devices go last as [[devices]] tables."""
    data = dataclasses.asdict(cfg)
    devices = data.pop("devices", [])
    lines = ["# Patchyard site configuration, written by the setup wizard.",
             "# Every key is explained in config/patchyard.example.toml.", ""]

    def table(name: str, values: dict[str, Any]) -> None:
        scalars = {k: v for k, v in values.items() if not isinstance(v, dict)}
        lines.append(f"[{name}]")
        lines.extend(f"{k} = {_toml_value(v)}" for k, v in scalars.items())
        lines.append("")
        for k, v in values.items():
            if isinstance(v, dict):
                table(f"{name}.{k}", v)

    for section, values in data.items():
        table(section, values)
    for dev in devices:
        opts = dev.pop("options", {})
        lines.append("[[devices]]")
        lines.extend(f"{k} = {_toml_value(v)}" for k, v in dev.items())
        if opts:
            lines.append("[devices.options]")
            lines.extend(f"{k} = {_toml_value(v)}" for k, v in opts.items())
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ build + finish
def build(answers: dict[str, Any], state: Path) -> Config:
    """Turn the wizard's answers into a validated Config (raises ConfigError)."""
    a = answers
    cfg = Config()
    cfg.paths.state = str(state)
    if os.environ.get("PATCHYARD_CODE"):
        cfg.paths.code = os.environ["PATCHYARD_CODE"]
    cfg.site.name = str(a.get("site_name") or "Patchyard").strip()[:80]
    cfg.site.url = str(a.get("site_url") or "").strip().rstrip("/")
    domain = str(a.get("domain") or "").strip()
    cfg.site.domain = ("." + domain.lstrip(".")) if domain else ""

    method = a.get("auth_method", "local")
    cfg.auth.method = method
    if method == "kerberos":
        cfg.auth.realm = str(a.get("realm") or "").strip().upper()
        cfg.auth.group = str(a.get("group") or "").strip()
        if not cfg.auth.group:
            raise ConfigError("domain sign-in needs the group whose members may sign in")
    elif method == "local":
        if not a.get("admin_user") or not a.get("admin_password"):
            raise ConfigError("create the first admin account (name and password)")

    backend = a.get("notify", "none")
    cfg.notify.backends = [] if backend == "none" else [backend]
    if backend == "ntfy":
        cfg.notify.ntfy = configmod.Ntfy(url=str(a.get("ntfy_url", "")).strip(),
                                         topic=str(a.get("ntfy_topic", "")).strip(),
                                         token_file=str(a.get("ntfy_token_file", "")).strip())
    elif backend == "webhook":
        cfg.notify.webhook.url = str(a.get("webhook_url", "")).strip()
    elif backend == "smtp":
        recipients = [r.strip() for r in str(a.get("smtp_to", "")).split(",") if r.strip()]
        cfg.notify.smtp = configmod.Smtp(host=str(a.get("smtp_host", "")).strip(),
                                         port=int(a.get("smtp_port") or 587),
                                         user=str(a.get("smtp_user", "")).strip(),
                                         password_file=str(a.get("smtp_password_file", "")).strip(),
                                         sender=str(a.get("smtp_sender", "")).strip(),
                                         recipients=recipients)

    cfg.web.bind = str(a.get("bind") or "127.0.0.1").strip()
    cfg.web.port = int(a.get("port") or 8790)
    if a.get("controller_host"):
        cfg.controller.host = str(a["controller_host"]).strip()
    configmod.validate(cfg)
    return cfg


def finish(answers: dict[str, Any], config_path: Path, state: Path) -> Config:
    """Validate, write the config file, create the first admin, retire the setup code."""
    cfg = build(answers, state)
    if config_path.exists():
        raise ConfigError(f"{config_path} already exists; setup has been completed")
    if cfg.auth.method == "local":
        Users(state).set(str(answers["admin_user"]).strip().lower(), str(answers["admin_password"]), create=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as fh:
        fh.write(toml_dumps(cfg))
    tmp.replace(config_path)
    configmod.load(config_path)                 # round-trip: what was written loads and validates
    (state / TOKEN_FILE).unlink(missing_ok=True)
    return cfg
